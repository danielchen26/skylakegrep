"""Drive ``multi_channel_search`` against the React benchmark DB.

Opens the pre-built React SQLite index, loads the 10 react tasks from
``benchmarks/cross_repo/react.json``, runs each through
``multi_channel_search`` (cosine ∪ symbol channel, RRF-fused), and
reports per-task hit/miss + aggregate K/10 + timing breakdown.

Usage:

    .venv/bin/python benchmarks/run_multi_channel_react.py \\
        --db /tmp/skylakegrep-rg-parity.sqlite \\
        --root /private/tmp/oss-bench/react

The script can also point at the django (``/tmp/sky-django.sqlite``)
or tokio (``/tmp/sky-tokio.sqlite``) bench DBs by passing
``--tasks benchmarks/cross_repo/django.json`` and the matching root.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from skylakegrep.src.embeddings import get_embedder
from skylakegrep.src.hybrid import lexical_candidate_paths
from skylakegrep.src.storage import populate_symbols, search as cosine_search
from skylakegrep.src.symbol_channel import multi_channel_search


# Default paths matching the reference benchmark setup.
DEFAULTS = {
    "react": {
        "db": "/tmp/skylakegrep-rg-parity.sqlite",
        "root": "/private/tmp/oss-bench/react",
        "tasks": "benchmarks/cross_repo/react.json",
    },
    "django": {
        "db": "/tmp/sky-django.sqlite",
        "root": "/private/tmp/oss-bench/django",
        "tasks": "benchmarks/cross_repo/django.json",
    },
    "tokio": {
        "db": "/tmp/sky-tokio.sqlite",
        "root": "/private/tmp/oss-bench/tokio",
        "tasks": "benchmarks/cross_repo/tokio.json",
    },
}


def hit(task: dict, paths: list[str]) -> bool:
    """Substring match: ``expected`` or any ``expected_alternatives`` is
    contained inside any returned path. Same convention as
    parity_vs_ripgrep.expected_hit and v0_5_repo_A_bench.hit."""
    accepted = [task["expected"], *task.get("expected_alternatives", [])]
    return any(any(a in p for p in paths if p) for a in accepted if a)


def ensure_symbols(conn: sqlite3.Connection, root: Path) -> tuple[int, int]:
    """Populate ``symbols`` if empty, returning ``(before, after)`` counts.

    Mirrors the ``_ensure_file_graph_populated`` pattern: best-effort,
    no-op when already populated.
    """
    before = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    if before == 0:
        populate_symbols(conn, root)
    after = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    return before, after


def cosine_only_top_k(
    conn: sqlite3.Connection,
    embedder,
    question: str,
    *,
    top_k: int,
    candidate_paths,
) -> tuple[list[dict], float]:
    """Run the cosine channel alone (rerank=True, multi_resolution off)
    so we have an apples-to-apples baseline number per task. Returns
    ``(results, latency_ms)``.
    """
    t0 = time.perf_counter()
    qv = embedder.embed(question)
    res = cosine_search(
        conn,
        qv,
        top_k=top_k,
        query_text=question,
        rerank=True,
        rerank_pool=30,
        candidate_paths=candidate_paths,
    )
    return res, (time.perf_counter() - t0) * 1000.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        choices=("react", "django", "tokio"),
        default="react",
        help="Which preset DB/root/tasks to use (default: react)",
    )
    parser.add_argument("--db", help="Override the DB path")
    parser.add_argument("--root", help="Override the repo root")
    parser.add_argument("--tasks", help="Override the tasks JSON path")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument(
        "--lexical-prefilter",
        dest="lexical_prefilter",
        action="store_true",
        default=True,
        help="Use ripgrep prefilter before cosine + rerank (default on)",
    )
    parser.add_argument(
        "--no-lexical-prefilter",
        dest="lexical_prefilter",
        action="store_false",
    )
    parser.add_argument(
        "--print-misses",
        action="store_true",
        help="List the top-K paths for missed tasks",
    )
    args = parser.parse_args()

    preset = DEFAULTS[args.repo]
    db_path = Path(args.db or preset["db"])
    root = Path(args.root or preset["root"]).resolve()
    tasks_path = Path(args.tasks or PROJECT_ROOT / preset["tasks"])

    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1
    if not root.exists():
        print(f"root not found: {root}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    chunks_n, files_n = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT file) FROM chunks"
    ).fetchone()
    print(f"DB: {db_path}")
    print(f"  chunks: {chunks_n} across {files_n} files")
    sym_before, sym_after = ensure_symbols(conn, root)
    print(f"  symbols: {sym_before} -> {sym_after}")

    tasks = json.loads(Path(tasks_path).read_text())
    print(f"Tasks: {len(tasks)} from {tasks_path}")

    embedder = get_embedder()
    fused_hits = 0
    cosine_hits = 0
    rows = []
    total_cosine_ms = 0.0
    total_symbol_ms = 0.0
    total_fuse_ms = 0.0
    total_latency_ms = 0.0
    cosine_only_total_ms = 0.0

    for task in tasks:
        question = task["question"]
        expected = task["expected"]
        # ripgrep prefilter (the same convention used by parity_vs_ripgrep)
        if args.lexical_prefilter:
            cands_abs = lexical_candidate_paths(question, root)
            cands = (
                {
                    str(Path(p).relative_to(root))
                    if Path(p).is_absolute()
                    else p
                    for p in cands_abs
                }
                if cands_abs
                else None
            )
        else:
            cands = None

        # Cosine-only baseline.
        co_results, co_ms = cosine_only_top_k(
            conn, embedder, question, top_k=args.top_k, candidate_paths=cands
        )
        co_paths = [r["path"] for r in co_results]
        co_hit = hit(task, co_paths)
        if co_hit:
            cosine_hits += 1
        cosine_only_total_ms += co_ms

        # Multi-channel.
        t0 = time.perf_counter()
        results, telem = multi_channel_search(
            conn,
            question,
            embedder=embedder,
            top_k=args.top_k,
            cosine_pool=30,
            symbol_pool=30,
            rerank=True,
            rerank_pool=30,
            candidate_paths=cands,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        total_latency_ms += latency_ms
        total_cosine_ms += telem["cosine_ms"]
        total_symbol_ms += telem["symbol_ms"]
        total_fuse_ms += telem["fuse_ms"]

        paths = [r["path"] for r in results]
        h = hit(task, paths)
        if h:
            fused_hits += 1
        marker_co = "HIT" if co_hit else "MISS"
        marker_fu = "HIT" if h else "MISS"
        print(
            f"  [{task['id']}] cosine={marker_co} fused={marker_fu} "
            f"cosine={telem['cosine_ms']:5.0f}ms "
            f"symbol={telem['symbol_ms']:5.1f}ms "
            f"fuse={telem['fuse_ms']:5.2f}ms "
            f"sym_n={telem['symbol_n']:2d} "
            f"sym_in_topk={telem['hits_in_symbol_channel']}"
        )
        if args.print_misses and not h:
            print(f"      expected: {expected}")
            for i, p in enumerate(paths[:5]):
                print(f"      top {i+1}: {p}")
        rows.append({"task": task["id"], "cosine_hit": co_hit, "fused_hit": h, **telem})

    n = len(tasks)
    print()
    print(f"Cosine-only:    {cosine_hits}/{n}  ({cosine_only_total_ms/n:.0f} ms/q)")
    print(f"Multi-channel:  {fused_hits}/{n}  ({total_latency_ms/n:.0f} ms/q)")
    print(
        f"  cosine={total_cosine_ms/n:.0f} ms  "
        f"symbol={total_symbol_ms/n:.1f} ms  "
        f"fuse={total_fuse_ms/n:.2f} ms"
    )
    return 0 if fused_hits >= cosine_hits else 2


if __name__ == "__main__":
    sys.exit(main())
