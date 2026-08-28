# SPDX-License-Identifier: Apache-2.0
"""Diagnostic: for the two hard-miss queries, show what cascade_search
actually returns top-10 and how the enriched crates/ai/ + app/src/billing/
chunks rank.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

os.environ.setdefault("OLLAMA_EMBED_MODEL", "nomic-embed-text")
os.environ.setdefault("SKYGREP_DB_PATH", "/tmp/<repo-D>_idx_p1.db")

import numpy as np

from skylakegrep.src.answerer import get_answerer
from skylakegrep.src.embeddings import get_embedder
from skylakegrep.src.storage import _file_level_pairs, init_db


HARD_MISSES = [
    ("Where does the assistant call a language model backend to answer a user question?", "crates/ai/"),
    ("Where is the user's subscription tier checked before unlocking paid features?", "app/src/billing/"),
]


def main() -> None:
    conn = init_db(Path(os.environ["SKYGREP_DB_PATH"]))
    embedder = get_embedder(role="query")

    for query, expected_dir in HARD_MISSES:
        print(f"\n{'='*72}\nQuery: {query}\nExpected dir: {expected_dir}\n{'='*72}")

        qv = np.array(embedder.embed(query), dtype=np.float32)

        # Corpus-wide file-mean cosine top-15
        pairs = _file_level_pairs(conn, qv, top_files=15, candidate_paths=None)
        print("\n  File-mean cosine top-15 (corpus-wide):")
        for i, (path, score) in enumerate(pairs, 1):
            short = path.replace("/path/to/Rust workspace/", "")
            in_target = expected_dir in path
            mark = "✓" if in_target else " "
            print(f"    {mark} {i:>2}.  {score:.4f}  {short}")

        # Where do enriched chunks in the target dir actually rank?
        rows = conn.execute(
            f"""
            SELECT chunks.id, chunks.file, chunks.description, vectors.embedding
            FROM chunks
            JOIN vectors ON vectors.id = chunks.id
            WHERE chunks.file LIKE ? AND chunks.enriched_at IS NOT NULL
            """,
            (f"%/{expected_dir}%",),
        ).fetchall()
        if not rows:
            print(f"\n  (no enriched chunks under {expected_dir} yet)")
            continue

        # Compute cosine for every enriched chunk
        scored = []
        qn = float(np.linalg.norm(qv))
        for row_id, file, desc, blob in rows:
            v = np.frombuffer(blob, dtype=np.float32)
            score = float(np.dot(v, qv) / (np.linalg.norm(v) * qn + 1e-8))
            scored.append((score, file, desc))
        scored.sort(reverse=True)

        print(f"\n  Top-5 enriched chunks under {expected_dir} (their cosine vs this query):")
        for score, file, desc in scored[:5]:
            short = file.replace("/path/to/Rust workspace/", "")
            print(f"    {score:.4f}  {short}")
            if desc:
                print(f"           desc: {desc[:160]}…" if len(desc) > 160 else f"           desc: {desc}")

        if scored:
            best = scored[0][0]
            # Compare: how many corpus chunks have score > best?
            ahead = conn.execute(
                "SELECT COUNT(*) FROM ("
                "  SELECT 1 FROM chunks JOIN vectors ON vectors.id = chunks.id"
                "  WHERE 1=1"
                ")"
            ).fetchone()[0]
            # Lazy estimate: count via file-mean instead — file-mean already computed
            ahead_files = sum(1 for _, s in pairs if s > best)
            print(f"\n  At file-mean level, {ahead_files} files have higher score than the best enriched chunk in {expected_dir}.")


if __name__ == "__main__":
    main()
