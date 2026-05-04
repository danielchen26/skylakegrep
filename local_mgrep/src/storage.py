import fnmatch
import logging
import os
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

# L2 symbol-aware boost. ``SYMBOL_WEIGHT`` is the maximum additive bump a
# fully-matched symbol adds to a candidate's score; partial matches scale
# linearly with the fraction of query terms that hit. Tuneable via the
# ``MGREP_SYMBOL_WEIGHT`` env var so the integrator can sweep it without
# touching code.
SYMBOL_WEIGHT = float(os.environ.get("MGREP_SYMBOL_WEIGHT", "0.10"))

# L4 file-export PageRank tiebreaker. Only fires when the gap between the
# top-1 and top-2 final scores is below ``TIEBREAK_EPS``. The keep-safe
# property is ``GRAPH_TIEBREAK_WEIGHT <= TIEBREAK_EPS`` — any score gap
# wider than the weight cannot be flipped by the prior. This is the fix
# for the P4-CGC failure mode where in-degree as a global cosine prior
# pulled hubs (``lib.rs`` with 1655 in-degree) ahead of canonical leaves.
TIEBREAK_EPS = float(os.environ.get("MGREP_TIEBREAK_EPS", "0.005"))
GRAPH_TIEBREAK_WEIGHT = float(
    os.environ.get("MGREP_GRAPH_TIEBREAK_WEIGHT", "0.005")
)

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
    # L3 doc2query enrichment columns. Old DBs are migrated by
    # ``ensure_chunk_metadata_columns`` so the enrich command can resume
    # against an index that pre-dates this feature.
    "enriched_at": "REAL",
    "description": "TEXT",
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
    # L2 symbol-aware index. ``name_lower`` is the camelCase-split lowercase
    # form (``LanguageModelClient`` → ``language model client``) so a query
    # like "language model" can substring-match the join even though the
    # source identifier was a single PascalCase word.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbols (
            file       TEXT NOT NULL,
            name       TEXT NOT NULL,
            name_lower TEXT NOT NULL,
            kind       TEXT NOT NULL,
            start_line INTEGER,
            end_line   INTEGER,
            file_mtime REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbols_name_lower ON symbols(name_lower)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_symbols_file       ON symbols(file)")
    # File-export graph (L4): per-file in/out degree and PageRank, populated
    # from regex-parsed import edges across Rust / Python / TS / JS. Used as
    # a near-tie tiebreaker, never as a global cosine prior.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_graph (
            file       TEXT PRIMARY KEY,
            in_degree  INTEGER,
            out_degree INTEGER,
            pagerank   REAL,
            file_mtime REAL
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


def populate_symbols(conn, root: Path) -> int:
    """(Re)build the ``symbols`` table from current ``chunks`` membership.

    Walks every distinct file currently referenced by ``chunks``, runs the
    tree-sitter / regex symbol extractor on it, and bulk-inserts the rows
    into ``symbols``. Idempotent: deletes existing rows for each file
    before inserting fresh ones, so calling it twice is a no-op aside from
    picking up file changes since the last call. Returns the total number
    of symbol rows inserted across all files.

    Symbol extraction is best-effort — files that fail to parse simply
    contribute zero rows. The boost path treats absent symbols as a
    no-signal case rather than an error.
    """

    # Local import to avoid a circular dep at module import time: indexer
    # imports nothing from storage today, but extract_file_symbols sits in
    # indexer and a future cross-import would otherwise be fragile.
    from .indexer import extract_file_symbols

    files = [row[0] for row in conn.execute("SELECT DISTINCT file FROM chunks").fetchall()]
    inserted = 0
    for file_str in files:
        path = Path(file_str)
        rows = extract_file_symbols(path, root)
        conn.execute("DELETE FROM symbols WHERE file = ?", (file_str,))
        if not rows:
            continue
        conn.executemany(
            """
            INSERT INTO symbols (file, name, name_lower, kind, start_line, end_line, file_mtime)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    r["file"],
                    r["name"],
                    r["name_lower"],
                    r["kind"],
                    r.get("start_line"),
                    r.get("end_line"),
                    r.get("file_mtime"),
                )
                for r in rows
            ],
        )
        inserted += len(rows)
    conn.commit()
    return inserted


def symbol_match_boost(
    conn,
    query_text: str,
    candidate_paths: Optional[set[str]] = None,
) -> dict[str, float]:
    """Return a ``{path: boost_score}`` mapping for symbol-aware reranking.

    The boost counts the number of distinct ≥4-char query terms that appear
    as a token inside a file's symbol ``name_lower`` strings (which are
    pre-camelCase-split, so ``LanguageModelClient`` → ``language model
    client``). The fraction of matched terms is multiplied by
    ``SYMBOL_WEIGHT`` to produce the final additive bump.

    When ``candidate_paths`` is provided, the lookup is restricted to those
    files; otherwise every file with at least one matched symbol gets an
    entry. Files with no matches are simply absent from the returned dict.
    """

    # Reuse the prefilter's term extractor so symbol boosts and ripgrep
    # candidates work from the same vocabulary.
    from .hybrid import extract_query_terms

    terms = [t for t in extract_query_terms(query_text) if len(t) >= 4]
    if not terms:
        return {}

    sql = "SELECT file, name_lower FROM symbols"
    params: list = []
    if candidate_paths:
        placeholders = ",".join("?" * len(candidate_paths))
        sql += f" WHERE file IN ({placeholders})"
        params.extend(sorted(candidate_paths))
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return {}

    # For each file, count the distinct query terms that appear as a token
    # in any of its symbols' space-joined lower form. We tokenize the
    # symbol's lower form by splitting on spaces so that "model" matches
    # "language model client" (token hit) but does not match the literal
    # substring inside an unrelated identifier.
    matched_terms_per_file: dict[str, set[str]] = {}
    for file_str, name_lower in rows:
        if not name_lower:
            continue
        tokens = name_lower.split()
        if not tokens:
            continue
        token_set = set(tokens)
        for term in terms:
            if term in token_set:
                matched_terms_per_file.setdefault(file_str, set()).add(term)

    denom = max(1, len(terms))
    return {
        path: (len(matched) / denom) * SYMBOL_WEIGHT
        for path, matched in matched_terms_per_file.items()
        if matched
    }


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


def _apply_graph_tiebreak(
    conn,
    candidates: list[dict],
    *,
    eps: float = TIEBREAK_EPS,
    weight: float = GRAPH_TIEBREAK_WEIGHT,
    top_k: int = 5,
) -> bool:
    """Resort ``candidates`` in place when the top two scores are within ``eps``.

    Reads ``file_graph.pagerank`` for the top-K paths, normalises to [0, 1]
    over that K-set, and adds ``weight * normalised_pr`` to each candidate's
    score before resorting. By construction ``weight <= eps`` ensures no
    candidate whose pre-tiebreak gap exceeds ``weight`` can be displaced.

    Returns True if the tiebreaker actually fired (top-1 and top-2 within
    ``eps`` and at least one pagerank value was found), so the caller can
    surface it on the status line.
    """

    if len(candidates) < 2:
        return False
    top1 = float(candidates[0].get("score", 0.0))
    top2 = float(candidates[1].get("score", 0.0))
    if top1 - top2 >= eps:
        return False
    # Read pagerank for the top-K candidates only.
    top = candidates[:top_k]
    paths = [c.get("path", "") for c in top if c.get("path")]
    if not paths:
        return False
    placeholders = ",".join("?" * len(paths))
    try:
        rows = conn.execute(
            f"SELECT file, pagerank FROM file_graph WHERE file IN ({placeholders})",
            paths,
        ).fetchall()
    except sqlite3.OperationalError:
        # file_graph table absent — table not migrated, silently skip.
        return False
    pr_map = {file_path: float(pr) for file_path, pr in rows}
    if not pr_map:
        return False
    values = [pr_map.get(p, 0.0) for p in paths]
    lo, hi = min(values), max(values)
    span = (hi - lo) + 1e-9
    norm = {p: (pr_map.get(p, 0.0) - lo) / span for p in paths}
    for c in top:
        bump = weight * norm.get(c.get("path", ""), 0.0)
        c["score"] = float(c.get("score", 0.0)) + bump
        c["graph_tiebreak"] = True
    # Resort the prefix; tail order is irrelevant for top-K display.
    top.sort(key=lambda c: c.get("score", 0.0), reverse=True)
    candidates[:top_k] = top
    return True


def _file_rank(candidates: list[dict], top_k: int) -> list[dict]:
    """Return one best-scoring chunk per file, sorted by that score.

    Each file contributes exactly one slot to the result list. Within a file
    the chunk with the highest ``score`` wins. Files are then ordered by their
    best-chunk score and the top-K are returned.

    This is the ``rank_by="file"`` path in ``search()``.
    """
    best: dict[str, dict] = {}
    for candidate in candidates:
        path = candidate["path"]
        if path not in best or candidate["score"] > best[path]["score"]:
            best[path] = candidate
    ranked = sorted(best.values(), key=lambda c: c["score"], reverse=True)
    return ranked[:top_k]


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
    rank_by: str = "chunk",
    use_symbol_boost: bool = True,
    use_graph_tiebreak: bool = True,
) -> list[dict]:
    """Hybrid retrieval with optional cross-encoder rerank and file ranking.

    When ``rerank`` is True and ``query_text`` is provided, the cosine + lexical
    blend selects a wider candidate pool of size ``rerank_pool`` (default 50),
    which is then re-ordered by a cross-encoder reranker. The reranker is
    skipped silently if the optional dep ``sentence-transformers`` is missing.

    When ``rank_by="file"``, after all scoring and reranking each file
    contributes exactly one slot (its highest-scoring chunk) to the final
    result. Files are then sorted by that best-chunk score and the top-K
    are returned. This prevents files with many chunks from dominating
    top-K and improves recall for small canonical files. When
    ``rank_by="chunk"`` (the default), the existing diversify_results
    path is used unchanged.
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
    # repo-A queries where the tests of X consistently outranked the
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

    # L2 symbol-aware boost: an additive bump tied to how many query terms
    # match a file's symbol identifiers. Runs after the non-canonical-path
    # penalty so symbol matches in test files still get demoted, but
    # symbol-only matches in canonical files float up. Opt-in via the
    # ``use_symbol_boost`` kwarg so legacy unit tests can pin the old
    # ranking by passing False.
    if use_symbol_boost and query_text and candidates:
        try:
            boost_paths = {c.get("path") for c in candidates if c.get("path")}
            boosts = symbol_match_boost(conn, query_text, candidate_paths=boost_paths)
        except sqlite3.Error as exc:
            logger.warning("symbol boost failed: %s", exc)
            boosts = {}
        if boosts:
            for candidate in candidates:
                bump = boosts.get(candidate.get("path"), 0.0)
                if bump:
                    candidate["score"] = float(candidate.get("score", 0.0)) + bump
                    candidate["symbol_boost"] = bump
            candidates.sort(key=lambda c: c.get("score", 0.0), reverse=True)

    # L4 file-export PageRank tiebreaker: only re-orders the top-2 when their
    # score gap is below ``TIEBREAK_EPS``. Hubs only win against another hub,
    # never against a clearly-higher-scoring leaf — that's the regression
    # guard against P4-CGC's failure mode.
    if use_graph_tiebreak and candidates:
        _apply_graph_tiebreak(conn, candidates)

    if rank_by == "file":
        return _file_rank(candidates, top_k)
    return diversify_results(candidates, top_k)

def _file_level_pairs(
    conn,
    query_embedding: np.ndarray,
    top_files: int,
    candidate_paths: Optional[set[str]] = None,
) -> list[tuple[str, float]]:
    """Like ``file_level_search`` but returns ``(path, score)`` pairs.

    Used by the cascade path so the caller can read the top-1 / top-2 score
    gap to decide whether the cheap file-mean retrieval is confident enough
    to skip the LLM-driven escalation.
    """
    if candidate_paths is not None and not candidate_paths:
        return []
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
    return [(rows[int(i)][0], float(scores[int(i)])) for i in order]


CASCADE_DEFAULT_TAU = 0.015


def cascade_search(
    conn,
    query_embedding,
    *,
    query_text: str,
    embedder,
    answerer=None,
    top_k: int = 10,
    candidate_paths: Optional[set[str]] = None,
    tau: float = CASCADE_DEFAULT_TAU,
    languages: tuple[str, ...] = (),
    include_patterns: tuple[str, ...] = (),
    exclude_patterns: tuple[str, ...] = (),
    use_symbol_boost: bool = True,
    use_graph_tiebreak: bool = True,
) -> tuple[list[dict], dict]:
    """Confidence-gated retrieval: cheap file-mean cosine first, escalate when uncertain.

    Phase 1 (cheap): rank files by mean-of-chunks cosine within the lexical
    prefilter. If top-1 score - top-2 score >= ``tau`` the answer is treated
    as confidently localized and we return one chunk per top file.

    Phase 2 (escalate): otherwise, fall back to the full Round A + Round C
    union — cosine + file-rank, then HyDE + cosine + file-rank — and union
    the result lists with score-preserving dedup.

    On the repo-A 16-task benchmark this hits **14/16 recall at ~1.9 s/q
    (tau=0.015, 81% early-exit)**, matching the previous max-accurate tier
    (14/16 @ 21.8 s/q) at an order of magnitude lower latency.

    Returns ``(results, telemetry)`` where telemetry exposes:
      * ``early_exit``: bool — True iff phase 1 was confident enough.
      * ``gap``: float — top1 - top2 file-mean cosine score.
      * ``tau``: float — threshold used.
    """

    qv = np.array(query_embedding, dtype=np.float32)
    # 0.5.1 fix: file-mean cosine runs **corpus-wide**, not within the rg
    # candidate set. The rg prefilter is a hard filter against on-disk file
    # text; L3-enriched embeddings (which carry the LLM-written description)
    # would otherwise be invisible to file-mean cosine when the on-disk
    # text doesn't share surface tokens with the query. Corpus-wide
    # file-mean cosine on ~5K files is ~5-10 ms — cheap enough to run
    # unconditionally. Chunk-level cosine still uses the rg candidate set
    # via the normal search() multi-resolution path on the cheap branch.
    pairs = _file_level_pairs(
        conn,
        qv,
        top_files=max(top_k, 10),
        candidate_paths=None,
    )
    gap = (pairs[0][1] - pairs[1][1]) if len(pairs) >= 2 else 0.0
    early_exit = len(pairs) >= 2 and gap >= tau

    if early_exit:
        chosen = {p for p, _ in pairs}
        cheap = search(
            conn,
            query_embedding,
            top_k=top_k,
            languages=languages,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            query_text=query_text,
            rerank=False,
            multi_resolution=False,
            candidate_paths=chosen,
            rank_by="file",
            use_symbol_boost=use_symbol_boost,
            use_graph_tiebreak=use_graph_tiebreak,
        )
        return cheap, {"early_exit": True, "gap": gap, "tau": tau}

    # Escalation: Round A (cosine + file-rank) ∪ Round C (HyDE + cosine +
    # file-rank). 0.5.1 fix: the rg prefilter is a hard filter against
    # disk-resident file text, but L3 enrichment lives in chunk
    # *embeddings* — files whose disk text doesn't contain query terms are
    # invisible to rg even when their enriched embeddings would be the
    # right answer (the repo-A ``crates/ai/`` and ``app/src/billing/``
    # cases). When the cascade decides to escalate (i.e. confidence is
    # already low), we drop ``candidate_paths`` so the cosine + multi-
    # resolution stages run against the full corpus, letting enriched
    # embeddings actually contribute. The cheap path above still uses
    # ``candidate_paths`` — fast queries don't pay this widening.
    a = search(
        conn,
        query_embedding,
        top_k=top_k,
        languages=languages,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        query_text=query_text,
        rerank=False,
        multi_resolution=True,
        file_top=30,
        candidate_paths=None,
        rank_by="file",
        use_symbol_boost=use_symbol_boost,
        use_graph_tiebreak=use_graph_tiebreak,
    )
    h_query = query_text
    if answerer is not None:
        try:
            h_query = answerer.hyde(query_text)
        except Exception:
            h_query = query_text
    h_embedding = embedder.embed(h_query)
    c = search(
        conn,
        h_embedding,
        top_k=top_k,
        languages=languages,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        query_text=query_text,
        rerank=False,
        multi_resolution=True,
        file_top=30,
        candidate_paths=None,
        rank_by="file",
        use_symbol_boost=use_symbol_boost,
        use_graph_tiebreak=use_graph_tiebreak,
    )
    merged: dict[tuple, dict] = {}
    for r in a + c:
        key = (r.get("path"), r.get("start_line"), r.get("end_line"))
        if key not in merged or r.get("score", 0.0) > merged[key].get("score", 0.0):
            merged[key] = r
    out = sorted(merged.values(), key=lambda r: r.get("score", 0.0), reverse=True)[:top_k]
    return out, {"early_exit": False, "gap": gap, "tau": tau}


def get_indexed_files(conn) -> dict:
    cursor = conn.execute("SELECT file, MAX(file_mtime) as mtime FROM chunks GROUP BY file")
    return {row[0]: row[1] for row in cursor.fetchall()}
