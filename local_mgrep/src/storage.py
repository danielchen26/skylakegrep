import sqlite3
import numpy as np
from pathlib import Path
from .config import get_config

def init_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            file TEXT, chunk TEXT, language TEXT, chunk_index INTEGER,
            file_mtime REAL
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS vectors (id INTEGER, embedding BLOB)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file ON chunks(file)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file_mtime ON chunks(file, file_mtime)")
    conn.commit()
    return conn

def store_chunk(conn, file: str, chunk: str, language: str, chunk_index: int, embedding: list[float], file_mtime: float = None):
    cursor = conn.execute(
        "INSERT INTO chunks (file, chunk, language, chunk_index, file_mtime) VALUES (?, ?, ?, ?, ?)",
        (file, chunk, language, chunk_index, file_mtime)
    )
    vec = np.array(embedding, dtype=np.float32)
    conn.execute("INSERT INTO vectors (id, embedding) VALUES (?, ?)",
                 (cursor.lastrowid, vec.tobytes()))
    conn.commit()

def store_chunks_batch(conn, chunks_data: list[dict]):
    for data in chunks_data:
        cursor = conn.execute(
            "INSERT INTO chunks (file, chunk, language, chunk_index, file_mtime) VALUES (?, ?, ?, ?, ?)",
            (data["file"], data["chunk"], data["language"], data["chunk_index"], data.get("file_mtime"))
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

def get_file_mtime(conn, file: str) -> float:
    cursor = conn.execute("SELECT MAX(file_mtime) FROM chunks WHERE file = ?", (file,))
    row = cursor.fetchone()
    return row[0] if row and row[0] else 0

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8)

def search(conn, query_embedding: list[float], top_k: int = 10) -> list[dict]:
    query_vec = np.array(query_embedding, dtype=np.float32)
    cursor = conn.execute("SELECT id, embedding FROM vectors")
    results = []
    for row in cursor:
        vec = np.frombuffer(row[1], dtype=np.float32)
        score = cosine_similarity(query_vec, vec)
        results.append((score, row[0]))
    results.sort(reverse=True)
    chunk_ids = [r[1] for r in results[:top_k]]
    if not chunk_ids:
        return []
    placeholders = ",".join("?" * len(chunk_ids))
    chunks = conn.execute(
        f"SELECT id, file, chunk, language FROM chunks WHERE id IN ({placeholders})",
        chunk_ids
    ).fetchall()
    id_to_chunk = {c[0]: c for c in chunks}
    return [{"id": r[1], "file": id_to_chunk[r[1]][1], "chunk": id_to_chunk[r[1]][2], "score": r[0]}
            for r in results[:top_k] if r[1] in id_to_chunk]

def get_indexed_files(conn) -> dict:
    cursor = conn.execute("SELECT file, MAX(file_mtime) as mtime FROM chunks GROUP BY file")
    return {row[0]: row[1] for row in cursor.fetchall()}