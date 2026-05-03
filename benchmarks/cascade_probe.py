"""Cascade probe: Round B with confidence early-exit, escalate uncertain queries.

Insight: Round B (file-mean cosine on rg-prefiltered candidates) hits 11/16 @
0.13 s/q standalone. Round A+C (cosine + HyDE union) hits 14/16 but costs
~7 s/q. If we can detect *which* queries are easy (B is right) vs uncertain (B
might be wrong), we pay HyDE only on uncertain queries — average latency drops
to ~2 s/q while keeping 14/16 recall.

Confidence proxy: top1_score - top2_score in Round B. A dominant top-1 means
the file-mean cosine is sure; close scores mean ambiguous.

Run:
    OLLAMA_EMBED_MODEL=nomic-embed-text MGREP_DB_PATH=/tmp/<repo-D>_idx_p1.db \
      .venv/bin/python benchmarks/cascade_probe.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path("/Users/tianchichen/Documents/github/local-mgrep")
WARP = Path("/Users/tianchichen/Documents/github/<repo-D>")
sys.path.insert(0, str(REPO))

os.environ.setdefault("OLLAMA_EMBED_MODEL", "nomic-embed-text")
os.environ.setdefault("MGREP_DB_PATH", "/tmp/<repo-D>_idx_p1.db")

import numpy as np

from local_mgrep.src.embeddings import get_embedder
from local_mgrep.src.hybrid import lexical_candidate_paths
from local_mgrep.src.storage import search

TASKS = json.loads((REPO / "benchmarks/cross_repo/<repo-D>.json").read_text())


def hit(expected: str, paths: list[str]) -> bool:
    return any(expected in p for p in paths)


def round_b_with_scores(
    conn, qv: np.ndarray, candidate_paths: set[str], top_files: int = 10
) -> list[tuple[str, float]]:
    """Return file-mean cosine top-N as (path, score) pairs."""
    if not candidate_paths:
        return []
    placeholders = ",".join("?" * len(candidate_paths))
    rows = conn.execute(
        f"SELECT file, embedding FROM files WHERE file IN ({placeholders})",
        sorted(candidate_paths),
    ).fetchall()
    if not rows:
        return []
    matrix = np.vstack([np.frombuffer(blob, dtype=np.float32) for _, blob in rows])
    if matrix.shape[1] != qv.shape[0]:
        return []
    qn = float(np.linalg.norm(qv))
    denom = np.linalg.norm(matrix, axis=1) * qn + 1e-8
    scores = matrix @ qv / denom
    order = np.argsort(-scores)[:top_files]
    return [(rows[int(i)][0], float(scores[int(i)])) for i in order]


def round_a_paths(conn, embedder, q: str, cands: set[str]) -> list[str]:
    res = search(
        conn,
        embedder.embed(q),
        top_k=10,
        query_text=q,
        rerank=False,
        multi_resolution=True,
        file_top=30,
        candidate_paths=cands,
        rank_by="file",
    )
    return [r["path"] for r in res]


def round_c_paths(conn, embedder, q: str, cands: set[str]) -> list[str]:
    from local_mgrep.src.answerer import get_answerer

    try:
        h_q = get_answerer().hyde(q)
    except Exception:
        h_q = q
    res = search(
        conn,
        embedder.embed(h_q),
        top_k=10,
        query_text=q,
        rerank=False,
        multi_resolution=True,
        file_top=30,
        candidate_paths=cands,
        rank_by="file",
    )
    return [r["path"] for r in res]


def cascade(conn, embedder, q: str, cands: set[str], tau: float) -> tuple[list[str], bool]:
    """Run Round B; if (top1 - top2) >= tau, return B; else escalate to A+C union.

    Returns (paths, early_exit_bool).
    """
    qv = np.array(embedder.embed(q), dtype=np.float32)
    b = round_b_with_scores(conn, qv, cands, top_files=10)
    if len(b) < 2:
        # Degenerate; cannot measure confidence — escalate.
        a = round_a_paths(conn, embedder, q, cands)
        c = round_c_paths(conn, embedder, q, cands)
        return _union(a, c), False
    gap = b[0][1] - b[1][1]
    if gap >= tau:
        return [p for p, _ in b], True
    # Uncertain — escalate.
    a = round_a_paths(conn, embedder, q, cands)
    c = round_c_paths(conn, embedder, q, cands)
    return _union(a, c), False


def _union(*lists: list[str]) -> list[str]:
    seen, out = set(), []
    for l in lists:
        for p in l:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def main() -> None:
    conn = sqlite3.connect("/tmp/<repo-D>_idx_p1.db")
    embedder = get_embedder(role="query")

    taus = [0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.05]

    # Pre-compute Round B once per query (deterministic), and prepare cands.
    print(f"Cascade probe over {len(TASKS)} <repo-D> tasks; sweeping tau ∈ {taus}\n")
    print(f"{'tau':>6}  {'recall':>7}  {'total_s':>8}  {'avg_s/q':>8}  {'#exit':>6}  {'exit%':>6}")

    for tau in taus:
        hits = 0
        early = 0
        total_t = 0.0
        misses = []
        for t in TASKS:
            q, exp = t["question"], t["expected"]
            t0 = time.perf_counter()
            cands = lexical_candidate_paths(q, WARP)
            paths, exited = cascade(conn, embedder, q, cands, tau)
            total_t += time.perf_counter() - t0
            if exited:
                early += 1
            if hit(exp, paths):
                hits += 1
            else:
                misses.append(exp)
        n = len(TASKS)
        print(
            f"{tau:>6.3f}  {hits:>3}/{n:>3}  {total_t:>7.2f}s  {total_t/n:>7.2f}s  "
            f"{early:>4}/{n:>3}  {100*early/n:>5.1f}%"
        )
        if misses:
            print(f"        misses: {misses}")


if __name__ == "__main__":
    main()
