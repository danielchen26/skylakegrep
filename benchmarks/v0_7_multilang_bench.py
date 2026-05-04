"""v0.7.0 multi-language layered benchmark.

Generalizes the v0.5 repo-A 16-task benchmark to three languages:

    * Rust    — repo-A                     (16 tasks, /tmp/<repo-D>_idx_p1.db legacy DB)
    * Python  — repo-B                      (12 tasks, per-project DB)
    * TypeScript — repo-C (12 tasks, per-project DB)

For each repo we run the same Tier A/B/C/D cascade as v0.5_<repo-D>_bench
(cascade only, +L2 symbol boost, +L4 graph tiebreaker, full 0.5.0) and report
recall, total / per-query latency, and early-exit rate.  An aggregate row at
the bottom sums recall and averages latency across all three benches so the
"does the cascade generalize beyond Rust" question has a one-number answer.

Run:
    OLLAMA_EMBED_MODEL=nomic-embed-text \
      .venv/bin/python benchmarks/v0_7_multilang_bench.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("OLLAMA_EMBED_MODEL", "nomic-embed-text")

from skylakegrep.src.answerer import get_answerer
from skylakegrep.src.code_graph import populate_graph_table
from skylakegrep.src.config import project_db_path
from skylakegrep.src.embeddings import get_embedder
from skylakegrep.src.hybrid import lexical_candidate_paths
from skylakegrep.src.storage import (
    cascade_search,
    init_db,
    populate_symbols,
)


@dataclass(frozen=True)
class Bench:
    name: str
    tasks_path: Path
    repo_path: Path
    db_path: Path  # explicit DB path (per-project for new benches, legacy for repo-A)


# Repo-A keeps its legacy /tmp index built under nomic-embed-text in earlier
# P-phases. repo-B and Repo-C use the standard per-project DB derived from
# ``project_db_path``.
BENCHES: list[Bench] = [
    Bench(
        name="repo-A (Rust)",
        tasks_path=REPO_ROOT / "benchmarks/cross_repo/repo-a.json",
        repo_path=Path("/path/to/repo-A"),
        db_path=Path("/tmp/<repo-D>_idx_p1.db"),
    ),
    Bench(
        name="repo-B (Python)",
        tasks_path=REPO_ROOT / "benchmarks/cross_repo/repo-b.json",
        repo_path=Path("/Users/tianchichen/Documents/GitHub/repo-B"),
        db_path=project_db_path(Path("/Users/tianchichen/Documents/GitHub/repo-B")),
    ),
    Bench(
        name="repo-C (TypeScript)",
        tasks_path=REPO_ROOT / "benchmarks/cross_repo/repo-c.json",
        repo_path=Path("/Users/tianchichen/Documents/GitHub/repo-C"),
        db_path=project_db_path(
            Path("/Users/tianchichen/Documents/GitHub/repo-C")
        ),
    ),
]


def hit(task: dict, results: list[dict]) -> bool:
    """A task hits if any returned path contains ``expected`` *or* any of
    the ``expected_alternatives`` substrings."""
    accepted = [task["expected"], *task.get("expected_alternatives", [])]
    paths = [r.get("path") or "" for r in results]
    return any(any(a in p for p in paths) for a in accepted)


def ensure_migrations(conn, repo: Path) -> None:
    n_sym = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    if n_sym == 0:
        print(f"  [migrate] populating symbols for {repo.name}…", flush=True)
        t0 = time.time()
        added = populate_symbols(conn, repo)
        print(f"    -> {added} symbols in {time.time() - t0:.1f}s", flush=True)
    n_graph = conn.execute("SELECT COUNT(*) FROM file_graph").fetchone()[0]
    if n_graph == 0:
        print(f"  [migrate] building file-export graph for {repo.name}…", flush=True)
        t0 = time.time()
        added = populate_graph_table(conn, repo)
        print(f"    -> {added} file_graph rows in {time.time() - t0:.1f}s", flush=True)


@dataclass
class TierResult:
    label: str
    n: int
    hits: int
    early: int
    total_t: float
    misses: list[str]


def run_tier(
    conn,
    repo: Path,
    tasks: list[dict],
    embedder,
    answerer,
    *,
    label: str,
    use_symbol_boost: bool,
    use_graph_tiebreak: bool,
) -> TierResult:
    n = len(tasks)
    hits = 0
    early = 0
    total_t = 0.0
    misses: list[str] = []
    for t in tasks:
        q = t["question"]
        cands = lexical_candidate_paths(q, repo)
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
            misses.append(t.get("id") or t["expected"])
    return TierResult(label, n, hits, early, total_t, misses)


def fmt_tier(tr: TierResult) -> str:
    avg = tr.total_t / max(tr.n, 1)
    return (
        f"  {tr.label:<24} : {tr.hits:>2}/{tr.n}  "
        f"{tr.total_t:6.2f}s total  {avg:5.2f}s/q  early-exit {tr.early}/{tr.n}"
    )


def run_one_bench(b: Bench) -> dict[str, TierResult]:
    print(f"\n=== {b.name} ===")
    print(f"  db    : {b.db_path}")
    print(f"  repo  : {b.repo_path}")
    if not b.db_path.exists():
        print(f"  [skip] DB not found, run `skygrep index` for {b.repo_path} first")
        return {}
    conn = init_db(b.db_path)
    ensure_migrations(conn, b.repo_path)
    n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_files = conn.execute("SELECT COUNT(DISTINCT file) FROM chunks").fetchone()[0]
    tasks = json.loads(b.tasks_path.read_text())
    print(f"  index : {n_chunks} chunks across {n_files} files")
    print(f"  tasks : {len(tasks)} from {b.tasks_path.name}")
    print()

    embedder = get_embedder(role="query")
    answerer = get_answerer()

    out: dict[str, TierResult] = {}
    out["A"] = run_tier(conn, b.repo_path, tasks, embedder, answerer,
                        label="A: cascade only",
                        use_symbol_boost=False, use_graph_tiebreak=False)
    print(fmt_tier(out["A"]))
    out["B"] = run_tier(conn, b.repo_path, tasks, embedder, answerer,
                        label="B: cascade + L2",
                        use_symbol_boost=True, use_graph_tiebreak=False)
    print(fmt_tier(out["B"]))
    out["C"] = run_tier(conn, b.repo_path, tasks, embedder, answerer,
                        label="C: cascade + L4",
                        use_symbol_boost=False, use_graph_tiebreak=True)
    print(fmt_tier(out["C"]))
    out["D"] = run_tier(conn, b.repo_path, tasks, embedder, answerer,
                        label="D: full 0.5/0.7 default",
                        use_symbol_boost=True, use_graph_tiebreak=True)
    print(fmt_tier(out["D"]))
    if out["D"].misses:
        print(f"      tier-D misses: {out['D'].misses}")
    return out


def main() -> None:
    print("v0.7.0 multi-language layered benchmark")
    print(f"models: embed={os.environ.get('OLLAMA_EMBED_MODEL')}  hyde={os.environ.get('OLLAMA_HYDE_MODEL', 'qwen2.5:3b (default)')}")

    all_results: dict[str, dict[str, TierResult]] = {}
    for b in BENCHES:
        all_results[b.name] = run_one_bench(b)

    # Aggregate ----------------------------------------------------------
    print()
    print("=" * 78)
    print("Aggregate (tier D = default cascade)")
    print("=" * 78)
    print(f"{'repo':<42} {'recall':>10} {'avg s/q':>10} {'early':>8}")
    print("-" * 78)
    total_hits = 0
    total_n = 0
    total_t = 0.0
    total_early = 0
    for b in BENCHES:
        r = all_results.get(b.name, {}).get("D")
        if r is None:
            print(f"{b.name:<42} {'-':>10} {'-':>10} {'-':>8}  (skipped)")
            continue
        avg = r.total_t / max(r.n, 1)
        print(f"{b.name:<42} {r.hits:>4}/{r.n:<5} {avg:>10.2f} {r.early:>4}/{r.n}")
        total_hits += r.hits
        total_n += r.n
        total_t += r.total_t
        total_early += r.early
    print("-" * 78)
    if total_n:
        agg_avg = total_t / total_n
        print(f"{'TOTAL':<42} {total_hits:>4}/{total_n:<5} {agg_avg:>10.2f} {total_early:>4}/{total_n}")

    # Per-tier comparison table -----------------------------------------
    print()
    print("=" * 78)
    print("Per-tier comparison (recall / avg s/q)")
    print("=" * 78)
    header = f"{'repo':<42} {'A':>11} {'B':>11} {'C':>11} {'D':>11}"
    print(header)
    print("-" * 78)
    for b in BENCHES:
        cells = []
        for tier in ("A", "B", "C", "D"):
            r = all_results.get(b.name, {}).get(tier)
            if r is None:
                cells.append("-")
            else:
                avg = r.total_t / max(r.n, 1)
                cells.append(f"{r.hits}/{r.n} {avg:.1f}s")
        print(f"{b.name:<42} {cells[0]:>11} {cells[1]:>11} {cells[2]:>11} {cells[3]:>11}")


if __name__ == "__main__":
    main()
