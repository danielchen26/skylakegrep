import fnmatch
import re
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np


LEXICAL_WEIGHT = 0.2
TOKEN_RE = re.compile(r"[a-z0-9]+")


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
    conn.commit()
    return conn

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
) -> list[dict]:
    query_vec = np.array(query_embedding, dtype=np.float32)
    params = []
    where = []
    if languages:
        placeholders = ",".join("?" * len(languages))
        where.append(f"chunks.language IN ({placeholders})")
        params.extend(languages)
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
    deduped = []
    seen = set()
    for index in ordered_indices:
        chunk = rows[int(index)]
        key = (chunk[1], chunk[4], chunk[5], chunk[2])
        if key in seen:
            continue
        seen.add(key)
        deduped.append({
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
        if len(deduped) >= top_k:
            break
    return deduped

def get_indexed_files(conn) -> dict:
    cursor = conn.execute("SELECT file, MAX(file_mtime) as mtime FROM chunks GROUP BY file")
    return {row[0]: row[1] for row in cursor.fetchall()}
