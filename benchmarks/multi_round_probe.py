"""Quick empirical probe: how does recall saturate as we add cheap retrieval rounds?

Each round queries the existing index with a different strategy and we union the
top-K paths. The goal is to discover whether 2-4 cheap rounds (each <2s) can
catch the canonical files our single-round pipeline misses on repo-A.

This is a probe, not a final pipeline. It writes no code into the search path —
it composes existing functions and reports the union recall after K rounds for
K=1..4. Run:

    OLLAMA_EMBED_MODEL=nomic-embed-text MGREP_DB_PATH=/tmp/<repo-D>_idx_p1.db \\
      .venv/bin/python benchmarks/multi_round_probe.py
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path("/Users/tianchichen/Documents/github/local-mgrep")
WARP = Path("/path/to/repo-A")
sys.path.insert(0, str(REPO))

import os

os.environ.setdefault("OLLAMA_EMBED_MODEL", "nomic-embed-text")
os.environ.setdefault("MGREP_DB_PATH", "/tmp/<repo-D>_idx_p1.db")

from local_mgrep.src.embeddings import get_embedder
from local_mgrep.src.hybrid import lexical_candidate_paths
from local_mgrep.src.storage import search

TASKS = json.loads((REPO / "benchmarks/cross_repo/repo-a.json").read_text())


def hit(expected: str, paths: list[str]) -> bool:
    return any(expected in p for p in paths)


def round_a_cosine(conn, embedder, q: str, candidate_paths: set[str]) -> list[str]:
    """Round A: prefilter + cosine + file-rank, no rerank, no HyDE."""
    res = search(
        conn,
        embedder.embed(q),
        top_k=10,
        query_text=q,
        rerank=False,
        multi_resolution=True,
        file_top=30,
        candidate_paths=candidate_paths,
        rank_by="file",
    )
    return [r["path"] for r in res]


def round_b_filemean(conn, embedder, q: str, candidate_paths: set[str]) -> list[str]:
    """Round B: file-level mean cosine ONLY (skip chunk cosine).
    Picks files whose mean embedding is closest to the query, returns 1
    chunk per file. Cheap (just file-level cosine), no extra Ollama call.
    """
    # Reuse the search() path with a high file_top + chunk-level off would be
    # tricky; instead, do file-level pick then look up the file's first chunk.
    from local_mgrep.src.storage import file_level_search

    qv = embedder.embed(q)
    import numpy as np

    qv = np.array(qv, dtype=np.float32)
    files = file_level_search(conn, qv, top_files=10, candidate_paths=candidate_paths)
    return files[:10]


def round_c_hyde(conn, embedder, q: str, candidate_paths: set[str]) -> list[str]:
    """Round C: HyDE-augmented cosine."""
    from local_mgrep.src.answerer import get_answerer

    try:
        h_query = get_answerer().hyde(q)
    except Exception:
        h_query = q
    res = search(
        conn,
        embedder.embed(h_query),
        top_k=10,
        query_text=q,
        rerank=False,
        multi_resolution=True,
        file_top=30,
        candidate_paths=candidate_paths,
        rank_by="file",
    )
    return [r["path"] for r in res]


def round_d_rg_rank(question: str, root: Path) -> list[str]:
    """Round D: rg term-frequency rank — file order by how many query terms hit."""
    rg = shutil.which("rg")
    if not rg:
        return []
    # Reuse hybrid.extract_query_terms via ripgrep parity script
    from local_mgrep.src.hybrid import extract_query_terms

    terms = extract_query_terms(question)
    file_hits: dict[str, int] = {}
    for term in terms:
        try:
            r = subprocess.run(
                [rg, "-il", "-F", term, str(root)], capture_output=True, text=True, timeout=10
            )
        except Exception:
            continue
        for line in r.stdout.splitlines():
            line = line.strip()
            if line:
                file_hits[line] = file_hits.get(line, 0) + 1
    return [f for f, _ in sorted(file_hits.items(), key=lambda kv: -kv[1])][:10]


def union_at(rounds: list[list[str]], k: int) -> list[str]:
    """Union the first k rounds and return a deduplicated path list."""
    seen = set()
    out = []
    for r in rounds[:k]:
        for p in r:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def main() -> None:
    conn = sqlite3.connect("/tmp/<repo-D>_idx_p1.db")
    embedder = get_embedder(role="query")

    times = {"A": 0.0, "B": 0.0, "C": 0.0, "D": 0.0}
    hits_a, hits_b, hits_c, hits_d = 0, 0, 0, 0
    union_hits = {1: 0, 2: 0, 3: 0, 4: 0}

    for t in TASKS:
        q, exp = t["question"], t["expected"]
        cands = lexical_candidate_paths(q, WARP)

        t0 = time.perf_counter()
        a = round_a_cosine(conn, embedder, q, cands)
        times["A"] += time.perf_counter() - t0

        t0 = time.perf_counter()
        b = round_b_filemean(conn, embedder, q, cands)
        times["B"] += time.perf_counter() - t0

        t0 = time.perf_counter()
        c = round_c_hyde(conn, embedder, q, cands)
        times["C"] += time.perf_counter() - t0

        t0 = time.perf_counter()
        d = round_d_rg_rank(q, WARP)
        times["D"] += time.perf_counter() - t0

        rounds = [a, b, c, d]
        if hit(exp, a):
            hits_a += 1
        if hit(exp, b):
            hits_b += 1
        if hit(exp, c):
            hits_c += 1
        if hit(exp, d):
            hits_d += 1
        for k in (1, 2, 3, 4):
            if hit(exp, union_at(rounds, k)):
                union_hits[k] += 1

    n = len(TASKS)
    print("per-round (16 tasks)")
    print(f"  A cosine + file-rank   : {hits_a}/{n}  total {times['A']:.2f}s ({times['A']/n:.2f}s/q)")
    print(f"  B file-mean only       : {hits_b}/{n}  total {times['B']:.2f}s ({times['B']/n:.2f}s/q)")
    print(f"  C HyDE-cosine          : {hits_c}/{n}  total {times['C']:.2f}s ({times['C']/n:.2f}s/q)")
    print(f"  D rg term-rank         : {hits_d}/{n}  total {times['D']:.2f}s ({times['D']/n:.2f}s/q)")
    print()
    print("union saturation curve (top-10 from each round, dedup)")
    cum_t = 0.0
    for k, label in zip((1, 2, 3, 4), ("A", "A+B", "A+B+C", "A+B+C+D")):
        cum_t += times[label[-1]]
        print(f"  K={k}  {label:8}  union recall {union_hits[k]}/{n}  total {cum_t:.2f}s ({cum_t/n:.2f}s/q)")


if __name__ == "__main__":
    main()
