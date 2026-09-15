# SPDX-License-Identifier: Apache-2.0
"""Production cascade benchmark — calls the real ``storage.cascade_search``
through the same code path that ``skygrep search --cascade`` uses, and verifies
Rust workspace recall matches the probe (14/16 @ ~1.9 s/q at tau=0.015).

Run:
    OLLAMA_EMBED_MODEL=nomic-embed-text SKYGREP_DB_PATH=/tmp/<repo-D>_idx_p1.db \
      .venv/bin/python benchmarks/cascade_production_bench.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WARP = Path(os.environ.get("SKYGREP_BENCH_RUST_REPO", "/path/to/Rust workspace"))
sys.path.insert(0, str(REPO))

os.environ.setdefault("OLLAMA_EMBED_MODEL", "nomic-embed-text")
os.environ.setdefault("SKYGREP_DB_PATH", "/tmp/<repo-D>_idx_p1.db")

from skylakegrep.src.answerer import get_answerer
from skylakegrep.src.embeddings import get_embedder
from skylakegrep.src.hybrid import lexical_candidate_paths
from skylakegrep.src.storage import cascade_search

TASKS = json.loads((REPO / "benchmarks/cross_repo/rust-workspace.json").read_text())


def hit(expected: str, results: list[dict]) -> bool:
    return any(expected in r.get("path", "") for r in results)


def main() -> None:
    conn = sqlite3.connect("/tmp/<repo-D>_idx_p1.db")
    embedder = get_embedder(role="query")
    answerer = get_answerer()

    n = len(TASKS)
    print(f"production cascade_search() bench over {n} Rust workspace tasks\n")
    print(f"{'tau':>6}  {'recall':>7}  {'total_s':>8}  {'avg_s/q':>8}  {'#exit':>6}  {'exit%':>6}")
    for tau in [0.0, 0.005, 0.01, 0.015, 0.02, 0.03]:
        hits = 0
        early = 0
        total_t = 0.0
        misses = []
        for t in TASKS:
            q, exp = t["question"], t["expected"]
            cands = lexical_candidate_paths(q, WARP)
            t0 = time.perf_counter()
            qv = embedder.embed(q)
            results, telem = cascade_search(
                conn,
                qv,
                query_text=q,
                embedder=embedder,
                answerer=answerer,
                top_k=10,
                candidate_paths=cands,
                tau=tau,
            )
            total_t += time.perf_counter() - t0
            if telem["early_exit"]:
                early += 1
            if hit(exp, results):
                hits += 1
            else:
                misses.append(exp)
        print(
            f"{tau:>6.3f}  {hits:>3}/{n:>3}  {total_t:>7.2f}s  {total_t/n:>7.2f}s  "
            f"{early:>4}/{n:>3}  {100*early/n:>5.1f}%"
        )
        if misses:
            print(f"        misses: {misses}")


if __name__ == "__main__":
    main()
