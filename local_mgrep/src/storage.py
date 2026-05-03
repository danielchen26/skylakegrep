import fnmatch
import logging
import re
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

from .reranker import DEFAULT_RERANK_POOL, rerank as cross_encoder_rerank


logger = logging.getLogger(__name__)


LEXICAL_WEIGHT = 0.2
MAX_RESULTS_PER_FILE = 2
TOKEN_RE = re.compile(r"[a-z0-9]+")

# Path patterns that almost always indicate a non-canonical file: tests,
# test fixtures, AI safety blocklists, integration helpers, generated bundles.
# A query like "where is X implemented" wants the implementation, not the
# tests of X or the blocklist that gates X. Matching these paths multiplies
# the final score by ``NON_CANONICAL_PATH_FACTOR`` so the penalty scales
# with the score's magnitude (cross-encoder scores can be 5-15, while cosine
# scores are 0-1; an absolute subtraction would over-penalise the latter
# and under-penalise the former).
_NON_CANONICAL_PATH_PATTERNS = (
    "_test.rs", "_tests.rs", "_test.py", "_tests.py",
    "/tests/", "/test/", "_test/", "_tests/",
    "/blocklist/", "/integration_testing/",
    "/__tests__/",
)
NON_CANONICAL_PATH_FACTOR = 0.5

_dim_warning_emitted = False


def _is_non_canonical(path: str) -> bool:
    lower = path.lower()
    return any(marker in lower for marker in _NON_CANONICAL_PATH_PATTERNS)


CHUNK_METADATA_COLUMNS = {
    "start_line": "INTEGER",
    "end_line": "INTEGER",
    "start_byte": "INTEGER",
    "end_byte": "INTEGER",
}


def ensure_chunk_metadata_columns(conn):
    existing = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    for column, column_type in CHUNK_METADATA_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE chunks ADD COLUMN {column} {column_type}")

def init_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            file TEXT, chunk TEXT, language TEXT, chunk_index INTEGER,
            file_mtime REAL,
            start_line INTEGER,
            end_line INTEGER,
            start_byte INTEGER,
            end_byte INTEGER
        )
    """)
    ensure_chunk_metadata_columns(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS vectors (id INTEGER, embedding BLOB)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file ON chunks(file)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_mtime ON chunks(file, file_mtime)")
    # File-level embedding cache for multi-resolution retrieval. The vector
    # is the mean of all chunk vectors of that file — built at index time
    # from the chunks table, no extra Ollama calls. Storing it lets the
    # search path do file-level retrieval first (small canonical files
    # compete fairly against large consumer files) before drilling into
    # chunks of the top-N files.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            file TEXT PRIMARY KEY,
            chunk_count INTEGER,
            embedding BLOB
        )
    """)
    conn.commit()
    return conn


def populate_file_embeddings(conn) -> int:
    """Rebuild the ``files`` table from current ``chunks`` + ``vectors``.

    For each file, computes the L2-normalised mean of its chunk vectors and
    stores it as the file-level embedding. Returns the number of files
    populated. Idempotent — drop and rebuild on every call so re-indexing
    leaves a consistent file aggregate. Skips zero-vectors silently.
    """

    files: dict[str, list[np.ndarray]] = {}
    rows = conn.execute(
        "SELECT chunks.file, vectors.embedding "
        "FROM chunks JOIN vectors ON vectors.id = chunks.id"
    ).fetchall()
    for file_path, blob in rows:
        vec = np.frombuffer(blob, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if norm == 0.0:
            continue
        files.setdefault(file_path, []).append(vec / norm)
    conn.execute("DELETE FROM files")
    for file_path, vectors in files.items():
        mean = np.mean(np.vstack(vectors), axis=0).astype(np.float32)
        mean_norm = float(np.linalg.norm(mean))
        if mean_norm > 0.0:
            mean = mean / mean_norm
        conn.execute(
            "INSERT INTO files (file, chunk_count, embedding) VALUES (?, ?, ?)",
            (file_path, len(vectors), mean.tobytes()),
        )
    conn.commit()
    return len(files)


def file_level_search(
    conn,
    query_embedding: np.ndarray,
    top_files: int = 30,
    candidate_paths: Optional[set[str]] = None,
) -> list[str]:
    """Return the top-N file paths by cosine similarity of file-level embeddings.

    When ``candidate_paths`` is given (typically the ripgrep prefilter
    output) the file-level cosine ranks within those files only; this is
    necessary so the prefilter and multi-resolution stages compose
    correctly — independently they would AND-intersect and could drop the
    canonical file when one stage's top-N excluded it.

    Returns an empty list when the ``files`` table is unpopulated, which is
    the signal to the caller that multi-resolution mode is unavailable for
    this index and chunk-only search should be used.
    """

    if candidate_paths:
        placeholders = ",".join("?" * len(candidate_paths))
        rows = conn.execute(
            f"SELECT file, embedding FROM files WHERE file IN ({placeholders})",
            sorted(candidate_paths),
        ).fetchall()
    else:
        rows = conn.execute("SELECT file, embedding FROM files").fetchall()
    if not rows:
        return []
    matrix = np.vstack([np.frombuffer(blob, dtype=np.float32) for _, blob in rows])
    if matrix.shape[1] != query_embedding.shape[0]:
        return []
    qn = float(np.linalg.norm(query_embedding))
    denom = np.linalg.norm(matrix, axis=1) * qn + 1e-8
    scores = matrix @ query_embedding / denom
    order = np.argsort(-scores)[:top_files]
    return [rows[int(i)][0] for i in order]

def store_chunk(
    conn,
    file: str,
    chunk: str,
    language: str,
    chunk_index: int,
    embedding: list[float],
    file_mtime: float = None,
    start_line: int = None,
    end_line: int = None,
    start_byte: int = None,
    end_byte: int = None,
):
    cursor = conn.execute(
        """
        INSERT INTO chunks (
            file, chunk, language, chunk_index, file_mtime,
            start_line, end_line, start_byte, end_byte
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (file, chunk, language, chunk_index, file_mtime, start_line, end_line, start_byte, end_byte)
    )
    vec = np.array(embedding, dtype=np.float32)
    conn.execute("INSERT INTO vectors (id, embedding) VALUES (?, ?)",
                 (cursor.lastrowid, vec.tobytes()))
    conn.commit()

def store_chunks_batch(conn, chunks_data: list[dict]):
    for data in chunks_data:
        cursor = conn.execute(
            """
            INSERT INTO chunks (
                file, chunk, language, chunk_index, file_mtime,
                start_line, end_line, start_byte, end_byte
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["file"],
                data["chunk"],
                data["language"],
                data["chunk_index"],
                data.get("file_mtime"),
                data.get("start_line"),
                data.get("end_line"),
                data.get("start_byte"),
                data.get("end_byte"),
            )
        )
        vec = np.array(data["embedding"], dtype=np.float32)
        conn.execute("INSERT INTO vectors (id, embedding) VALUES (?, ?)",
                     (cursor.lastrowid, vec.tobytes()))
    conn.commit()

def delete_file_chunks(conn, file: str):
    cursor = conn.execute("SELECT id FROM chunks WHERE file = ?", (file,))
    ids = [row[0] for row in cursor.fetchall()]
    if ids:
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM vectors WHERE id IN ({placeholders})", ids)
        conn.execute("DELETE FROM chunks WHERE file = ?", (file,))
        conn.commit()

def delete_missing_files(conn, current_files: set[str], root: Path) -> list[str]:
    root_path = root.resolve()
    deleted = []
    for file_path in get_indexed_files(conn):
        indexed_path = Path(file_path).resolve()
        try:
            indexed_path.relative_to(root_path)
        except ValueError:
            continue
        if file_path not in current_files:
            delete_file_chunks(conn, file_path)
            deleted.append(file_path)
    return deleted

def get_file_mtime(conn, file: str) -> float:
    cursor = conn.execute("SELECT MAX(file_mtime) FROM chunks WHERE file = ?", (file,))
    row = cursor.fetchone()
    return row[0] if row and row[0] else 0

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8)


def tokenize_search_text(text: str) -> list[str]:
    camel_spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    return TOKEN_RE.findall(camel_spaced.lower())


def lexical_score(query_text: str, path: str, chunk: str) -> float:
    query_tokens = tokenize_search_text(query_text)
    if not query_tokens:
        return 0.0
    query_terms = list(dict.fromkeys(query_tokens))
    target_tokens = tokenize_search_text(f"{path} {chunk}")
    target_terms = set(target_tokens)
    if not target_terms:
        return 0.0
    term_score = sum(1 for term in query_terms if term in target_terms) / len(query_terms)
    normalized_query = " ".join(query_tokens)
    normalized_target = " ".join(target_tokens)
    phrase_score = 1.0 if normalized_query and normalized_query in normalized_target else 0.0
    return min(1.0, (term_score * 0.75) + (phrase_score * 0.25))


def combine_scores(semantic_score: float, lexical_score_value: float) -> float:
    return (semantic_score * (1.0 - LEXICAL_WEIGHT)) + (lexical_score_value * LEXICAL_WEIGHT)


def diversify_results(candidates: list[dict], top_k: int, max_per_file: int = MAX_RESULTS_PER_FILE) -> list[dict]:
    if top_k <= 0:
        return []
    selected = []
    overflow = []
    file_counts = {}
    for candidate in candidates:
        path = candidate["path"]
        if file_counts.get(path, 0) < max_per_file:
            selected.append(candidate)
            file_counts[path] = file_counts.get(path, 0) + 1
            if len(selected) >= top_k:
                return selected
        else:
            overflow.append(candidate)
    for candidate in overflow:
        selected.append(candidate)
        if len(selected) >= top_k:
            break
    return selected


def path_matches(path: str, include_patterns: tuple[str, ...], exclude_patterns: tuple[str, ...]) -> bool:
    basename = Path(path).name
    if include_patterns and not any(
        fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern)
        for pattern in include_patterns
    ):
        return False
    if exclude_patterns and any(
        fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(basename, pattern)
        for pattern in exclude_patterns
    ):
        return False
    return True


def search(
    conn,
    query_embedding: list[float],
    top_k: int = 10,
    languages: tuple[str, ...] = (),
    include_patterns: tuple[str, ...] = (),
    exclude_patterns: tuple[str, ...] = (),
    query_text: Optional[str] = None,
    semantic_only: bool = False,
    rerank: bool = False,
    rerank_pool: int = DEFAULT_RERANK_POOL,
    rerank_model: Optional[str] = None,
    multi_resolution: bool = False,
    file_top: int = 30,
    candidate_paths: Optional[set[str]] = None,
) -> list[dict]:
    """Hybrid retrieval with optional cross-encoder rerank.

    When ``rerank`` is True and ``query_text`` is provided, the cosine + lexical
    blend selects a wider candidate pool of size ``rerank_pool`` (default 50),
    which is then re-ordered by a cross-encoder reranker. The reranker is
    skipped silently if the optional dep ``sentence-transformers`` is missing.
    """

    global _dim_warning_emitted
    query_vec = np.array(query_embedding, dtype=np.float32)
    params = []
    where = []
    if languages:
        placeholders = ",".join("?" * len(languages))
        where.append(f"chunks.language IN ({placeholders})")
        params.extend(languages)
    # Lexical prefilter (highest-priority candidate filter): when the caller
    # passes a non-empty ``candidate_paths`` set — typically the output of
    # ``hybrid.lexical_candidate_paths`` running ripgrep against the index
    # root — we restrict the chunk scan to those files. ripgrep's recall on
    # code-search benchmarks tends to be at the absolute ceiling, so this
    # narrows the search space from tens of thousands of chunks to a few
    # hundred without losing the answer.
    if candidate_paths:
        placeholders = ",".join("?" * len(candidate_paths))
        where.append(f"chunks.file IN ({placeholders})")
        params.extend(sorted(candidate_paths))
    # Multi-resolution stage 1: pick the top-N files by file-level cosine,
    # then restrict chunk-level retrieval to those files only. This stops
    # large consumer files (50 chunks of partial matches) from drowning out
    # small canonical files (1 chunk in `crates/X/lib.rs`) at the chunk
    # stage. The file-level vector is the L2-normalised mean of chunk
    # vectors, computed once at index time by ``populate_file_embeddings``.
    if multi_resolution:
        top_files = file_level_search(
            conn,
            query_vec,
            top_files=file_top,
            candidate_paths=candidate_paths,
        )
        if top_files:
            placeholders = ",".join("?" * len(top_files))
            where.append(f"chunks.file IN ({placeholders})")
            params.extend(top_files)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = conn.execute(
        f"""
        SELECT chunks.id, file, chunk, language, start_line, end_line,
               start_byte, end_byte, embedding
        FROM chunks JOIN vectors ON vectors.id = chunks.id
        {where_clause}
        """,
        params,
    ).fetchall()
    rows = [
        row for row in rows
        if path_matches(row[1], include_patterns, exclude_patterns)
    ]
    if not rows:
        return []
    matrix = np.vstack([np.frombuffer(row[8], dtype=np.float32) for row in rows])
    if matrix.shape[1] != query_vec.shape[0] and not _dim_warning_emitted:
        logger.warning(
            "embedding dim mismatch: query is %d-d but the index stores %d-d "
            "vectors. The current results will be wrong. Re-index with the "
            "current model: mgrep index <repo> --reset",
            query_vec.shape[0],
            matrix.shape[1],
        )
        _dim_warning_emitted = True
        return []
    denom = np.linalg.norm(matrix, axis=1) * np.linalg.norm(query_vec) + 1e-8
    semantic_scores = matrix @ query_vec / denom
    lexical_scores = np.zeros(len(rows), dtype=np.float32)
    scores = semantic_scores
    if query_text and not semantic_only:
        lexical_scores = np.array(
            [lexical_score(query_text, row[1], row[2]) for row in rows],
            dtype=np.float32,
        )
        scores = np.array(
            [
                combine_scores(float(semantic), float(lexical))
                for semantic, lexical in zip(semantic_scores, lexical_scores)
            ],
            dtype=np.float32,
        )
    ordered_indices = np.argsort(-scores)
    candidates = []
    seen = set()
    for index in ordered_indices:
        chunk = rows[int(index)]
        key = (chunk[1], chunk[4], chunk[5], chunk[2])
        if key in seen:
            continue
        seen.add(key)
        candidates.append({
            "id": chunk[0],
            "file": chunk[1],
            "path": chunk[1],
            "chunk": chunk[2],
            "snippet": chunk[2],
            "language": chunk[3],
            "start_line": chunk[4],
            "end_line": chunk[5],
            "start_byte": chunk[6],
            "end_byte": chunk[7],
            "score": float(scores[int(index)]),
            "semantic_score": float(semantic_scores[int(index)]),
            "lexical_score": float(lexical_scores[int(index)]),
        })

    if rerank and query_text and candidates:
        # The reranker re-orders the top ``rerank_pool`` and overwrites
        # ``score`` with the cross-encoder relevance score. Diversification
        # then runs on the reranked order, so the per-file cap still applies.
        candidates = cross_encoder_rerank(
            query_text,
            candidates,
            pool=rerank_pool,
            top_k=None,
            model_name=rerank_model,
        )

    # Non-canonical-path penalty: a multiplicative factor applied AFTER any
    # rerank step so it composes with both the cosine and the cross-encoder
    # signals. Files matching ``_test.rs`` / ``/blocklist/`` / etc. are rarely
    # the canonical answer to "where is X implemented", and removing them
    # from the top of the result list directly closes the recall gap on
    # warp queries where the tests of X consistently outranked the
    # implementation of X.
    penalised = False
    for candidate in candidates:
        if _is_non_canonical(candidate.get("path", "")):
            current = float(candidate.get("score", 0.0))
            # Multiplicative on positives, additive shift on negatives so a
            # negative cross-encoder score still moves further down rather
            # than closer to zero (which would *raise* its rank).
            candidate["score"] = (
                current * NON_CANONICAL_PATH_FACTOR
                if current >= 0
                else current - 1.0
            )
            candidate["non_canonical"] = True
            penalised = True
    if penalised:
        candidates.sort(key=lambda c: c.get("score", 0.0), reverse=True)

    return diversify_results(candidates, top_k)

def get_indexed_files(conn) -> dict:
    cursor = conn.execute("SELECT file, MAX(file_mtime) as mtime FROM chunks GROUP BY file")
    return {row[0]: row[1] for row in cursor.fetchall()}
