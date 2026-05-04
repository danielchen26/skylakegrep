"""v0.5.0 16-task Rust layered benchmark.

Measures the recall + latency contribution of each retrieval layer added
in 0.5.0 vs the 0.4.1 baseline:

    Tier A: 0.4.1 cascade (rg prefilter + cosine + file-rank, no L2/L4)
    Tier B: + L2 symbol boost
    Tier C: + L4 PageRank tiebreaker
    Tier D: + both L2 and L4 (the actual 0.5.0 default)
    Tier E: + L3 doc2query enrichment (only if --enriched and the index
            was already enriched by ``skygrep enrich``)

The script reuses the existing ``/tmp/<repo-D>_idx_p1.db`` index (built under
nomic-embed-text in earlier P-phases) and migrates it in-place to the
0.5.0 schema (adds ``symbols`` + ``file_graph`` tables; existing chunks
are unchanged). The benchmark *itself* never re-embeds anything, so the
one-time migration is the only setup cost.

Run:
    OLLAMA_EMBED_MODEL=nomic-embed-text SKYGREP_DB_PATH=/tmp/<repo-D>_idx_p1.db \
      .venv/bin/python benchmarks/v0_5_<repo-D>_bench.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path("/Users/tianchichen/Documents/github/skylakegrep")
WARP = Path("/path/to/Rust workspace")
sys.path.insert(0, str(REPO))

os.environ.setdefault("OLLAMA_EMBED_MODEL", "nomic-embed-text")
os.environ.setdefault("SKYGREP_DB_PATH", "/tmp/<repo-D>_idx_p1.db")

from skylakegrep.src.answerer import get_answerer
from skylakegrep.src.code_graph import populate_graph_table
from skylakegrep.src.embeddings import get_embedder
from skylakegrep.src.hybrid import lexical_candidate_paths
from skylakegrep.src.storage import (
    cascade_search,
    init_db,
    populate_symbols,
)

TASKS = json.loads((REPO / "benchmarks/cross_repo/rust-workspace.json").read_text())


def hit(task: dict, results: list[dict]) -> bool:
    """A task hits if any returned path contains ``expected`` *or* any of
    the ``expected_alternatives`` substrings (set when the original label
    is too narrow). See ground-truth notes in rust-workspace.json for the
    re-labelled tasks."""
    accepted = [task["expected"], *task.get("expected_alternatives", [])]
    paths = [r.get("path") or "" for r in results]
    return any(any(a in p for p in paths) for a in accepted)


def ensure_migrations(conn) -> None:
    n_sym = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    if n_sym == 0:
        print("populating symbols table (one-time)…", flush=True)
        t0 = time.time()
        added = populate_symbols(conn, WARP)
        print(f"  → {added} symbols in {time.time() - t0:.1f}s")
    n_graph = conn.execute("SELECT COUNT(*) FROM file_graph").fetchone()[0]
    if n_graph == 0:
        print("building file-export graph (one-time)…", flush=True)
        t0 = time.time()
        added = populate_graph_table(conn, WARP)
        print(f"  → {added} file_graph rows in {time.time() - t0:.1f}s")


def run_tier(
    conn,
    embedder,
    answerer,
    *,
    label: str,
    use_symbol_boost: bool,
    use_graph_tiebreak: bool,
) -> None:
    n = len(TASKS)
    hits = 0
    early = 0
    total_t = 0.0
    misses = []
    for t in TASKS:
        q = t["question"]
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
            use_symbol_boost=use_symbol_boost,
            use_graph_tiebreak=use_graph_tiebreak,
        )
        total_t += time.perf_counter() - t0
        if telem.get("early_exit"):
            early += 1
        if hit(t, results):
            hits += 1
        else:
            misses.append(t["expected"])
    print(
        f"  {label:<32} : {hits}/{n}  total {total_t:.2f}s  ({total_t/n:.2f}s/q)  "
        f"early-exit {early}/{n}"
    )
    if misses:
        print(f"      misses: {misses}")


def main() -> None:
    conn = init_db(Path(os.environ["SKYGREP_DB_PATH"]))
    ensure_migrations(conn)
    print()
    print(f"v0.5.0 Rust workspace benchmark over {len(TASKS)} tasks "
          f"(index has {conn.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]} chunks)\n")
    embedder = get_embedder(role="query")
    answerer = get_answerer()

    print("Tier A — 0.4.1 baseline (cascade only, L2 off, L4 off)")
    run_tier(conn, embedder, answerer,
             label="cascade only",
             use_symbol_boost=False, use_graph_tiebreak=False)

    print("\nTier B — + L2 symbol boost")
    run_tier(conn, embedder, answerer,
             label="cascade + L2",
             use_symbol_boost=True, use_graph_tiebreak=False)

    print("\nTier C — + L4 graph tiebreaker (no L2)")
    run_tier(conn, embedder, answerer,
             label="cascade + L4",
             use_symbol_boost=False, use_graph_tiebreak=True)

    print("\nTier D — full 0.5.0 (cascade + L2 + L4)")
    run_tier(conn, embedder, answerer,
             label="cascade + L2 + L4",
             use_symbol_boost=True, use_graph_tiebreak=True)

    n_enriched = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE enriched_at IS NOT NULL"
    ).fetchone()[0]
    print(f"\n[L3 enrichment status: {n_enriched} chunks enriched]")
    if n_enriched == 0:
        print("  → run `SKYGREP_DB_PATH=/tmp/<repo-D>_idx_p1.db skygrep enrich` "
              "from the Rust workspace directory to add Tier E to this benchmark.")


if __name__ == "__main__":
    main()
