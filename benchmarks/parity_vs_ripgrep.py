"""Real-ripgrep vs local-mgrep agent context benchmark.

This is a tighter version of `agent_context_benchmark.py`: instead of
simulating the grep agent in Python, it actually shells out to
`rg` (ripgrep) for every term search, parses ripgrep's JSON output,
and compares context-token usage and expected-file recall against a
single `mgrep search` per task.

The mgrep agent path is identical to `agent_context_benchmark.mgrep_agent_context`
so the comparison is apples-to-apples on the retrieval side.

Usage:
  .venv/bin/python benchmarks/parity_vs_ripgrep.py --top-k 10 --summary-only
  .venv/bin/python benchmarks/parity_vs_ripgrep.py --root ../warp --tasks tasks.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from local_mgrep.src.indexer import collect_indexable_files

from benchmarks.agent_context_benchmark import (
    DEFAULT_TASKS,
    extract_terms,
    mgrep_agent_context,
    safe_ratio,
)
from benchmarks.token_savings import (
    approximate_tokens,
    build_index,
    collect_source_doc_files,
    count_files,
    is_benchmark_ignored,
)


def ensure_ripgrep() -> str:
    rg = shutil.which("rg")
    if not rg:
        sys.exit(
            "ripgrep is required for this benchmark. "
            "Install it with `brew install ripgrep` or your package manager."
        )
    return rg


def rg_agent_context(
    rg_bin: str,
    question: str,
    root: Path,
    max_terms: int,
    max_matches_per_term: int,
    context_lines: int,
    chars_per_token: int,
) -> dict[str, object]:
    """Issue one `rg` invocation per extracted query term.

    Mirrors how a coding agent would use ripgrep before answering a
    natural-language question: pull terms, search each, attach context.
    """
    started = time.perf_counter()
    terms = extract_terms(question, max_terms=max_terms)
    sections: list[str] = []
    paths_seen: set[str] = set()
    tool_calls = 0

    for term in terms:
        tool_calls += 1
        cmd = [
            rg_bin,
            "--json",
            "-i",  # case-insensitive (mirrors simulated agent)
            "-F",  # literal string, no regex
            "--max-count", str(max_matches_per_term),
            "-C", str(context_lines),
            "--",
            term,
            str(root),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode not in (0, 1):
            continue  # rg returns 0 on match, 1 on no match, 2 on error
        if not proc.stdout.strip():
            continue

        section_lines = [f"## rg -F -i {term!r}"]
        match_count = 0
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            data = event.get("data", {})
            if etype in ("match", "context"):
                path_obj = data.get("path", {})
                rel_path = path_obj.get("text", "")
                if not rel_path:
                    continue
                # Make path relative to root for stable comparison
                try:
                    relative = str(Path(rel_path).resolve().relative_to(root))
                except ValueError:
                    relative = rel_path
                if etype == "match":
                    paths_seen.add(relative)
                    match_count += 1
                line_no_data = data.get("line_number")
                line_text = data.get("lines", {}).get("text", "").rstrip("\n")
                marker = ":" if etype == "match" else "-"
                section_lines.append(f"{relative}{marker}{line_no_data}{marker}{line_text}")
        if match_count:
            sections.append("\n".join(section_lines))

    payload = "\n\n".join(sections) if sections else "NO_MATCHES"
    return {
        "tool_calls": tool_calls,
        "paths": sorted(paths_seen),
        "context_chars": len(payload),
        "context_tokens": approximate_tokens(payload, chars_per_token),
        "latency_seconds": round(time.perf_counter() - started, 3),
    }


def load_tasks(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return DEFAULT_TASKS
    return json.loads(path.read_text(encoding="utf-8"))


def expected_hit(expected: str, paths: list[str]) -> bool:
    """Substring match: any returned path contains the expected token.

    Allows expected to be either an exact file path (e.g.
    ``local_mgrep/src/storage.py``) or a directory prefix (e.g.
    ``crates/ai/``). The latter is useful for cross-repo tasks where
    the relevant chunk may live anywhere inside a feature crate.
    """
    return any(expected in path for path in paths)


def benchmark(args: argparse.Namespace) -> dict[str, object]:
    rg_bin = ensure_ripgrep()
    root = Path(args.root).resolve()
    db_path = (
        Path(args.db_path)
        if args.db_path
        else Path(tempfile.gettempdir()) / "local-mgrep-rg-parity.sqlite"
    )

    indexed_files = [
        p for p in collect_indexable_files(root) if not is_benchmark_ignored(p, root)
    ]
    indexed_corpus = count_files(indexed_files, args.chars_per_token)
    source_doc_corpus = count_files(
        collect_source_doc_files(root, {".py", ".md", ".toml"}),
        args.chars_per_token,
    )

    conn, index_seconds = build_index(
        root,
        db_path,
        batch_size=args.batch_size,
        reuse_existing=getattr(args, "reuse_index", False),
    )
    chunks, indexed_db_files = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT file) FROM chunks"
    ).fetchone()

    rows: list[dict[str, object]] = []
    for task in load_tasks(args.tasks):
        expected = task["expected"]
        rg_result = rg_agent_context(
            rg_bin,
            task["question"],
            root,
            max_terms=args.rg_max_terms,
            max_matches_per_term=args.rg_max_matches_per_term,
            context_lines=args.rg_context_lines,
            chars_per_token=args.chars_per_token,
        )
        mgrep_result = mgrep_agent_context(
            conn,
            task["question"],
            top_k=args.top_k,
            chars_per_token=args.chars_per_token,
            rerank=getattr(args, "rerank", True),
            rerank_pool=getattr(args, "rerank_pool", 50),
            hyde=getattr(args, "hyde", False),
            multi_resolution=getattr(args, "multi_resolution", False),
            file_top=getattr(args, "file_top", 30),
            daemon_url=getattr(args, "daemon_url", None),
            lexical_prefilter=getattr(args, "lexical_prefilter", True),
            lexical_root=root if getattr(args, "lexical_prefilter", True) else None,
            lexical_min_candidates=getattr(args, "lexical_min_candidates", 2),
        )
        rg_total = (
            args.fixed_prompt_tokens
            + args.final_answer_tokens
            + int(rg_result["context_tokens"])
        )
        mgrep_total = (
            args.fixed_prompt_tokens
            + args.final_answer_tokens
            + int(mgrep_result["context_tokens"])
        )
        rows.append(
            {
                "id": task["id"],
                "question": task["question"],
                "expected": expected,
                "rg": {
                    **rg_result,
                    "hit": expected_hit(expected, rg_result["paths"]),
                    "estimated_total_tokens": rg_total,
                },
                "mgrep": {
                    **mgrep_result,
                    "hit": expected_hit(expected, mgrep_result["paths"]),
                    "estimated_total_tokens": mgrep_total,
                },
                "context_token_reduction_x": safe_ratio(
                    float(rg_result["context_tokens"]),
                    float(mgrep_result["context_tokens"]),
                ),
                "estimated_total_token_reduction_x": safe_ratio(rg_total, mgrep_total),
            }
        )

    rg_context = sum(int(row["rg"]["context_tokens"]) for row in rows)
    mgrep_context = sum(int(row["mgrep"]["context_tokens"]) for row in rows)
    rg_total = sum(int(row["rg"]["estimated_total_tokens"]) for row in rows)
    mgrep_total = sum(int(row["mgrep"]["estimated_total_tokens"]) for row in rows)

    return {
        "definition": {
            "benchmark_type": "real-ripgrep agent context vs local-mgrep top-k",
            "rg_agent": "one `rg --json -F -i -C 2 TERM ROOT` invocation per extracted query term",
            "mgrep_agent": "one semantic local-mgrep top-k search per task",
            "token_note": "tokens approximate as chars/4; estimated totals add fixed prompt and final-answer overhead",
        },
        "tooling": {
            "rg_path": rg_bin,
            "rg_version": subprocess.run(
                [rg_bin, "--version"], capture_output=True, text=True
            ).stdout.split("\n", 1)[0],
        },
        "parameters": {
            "tasks": len(rows),
            "top_k": args.top_k,
            "rg_max_terms": args.rg_max_terms,
            "rg_max_matches_per_term": args.rg_max_matches_per_term,
            "rg_context_lines": args.rg_context_lines,
            "fixed_prompt_tokens": args.fixed_prompt_tokens,
            "final_answer_tokens": args.final_answer_tokens,
            "rerank": getattr(args, "rerank", True),
            "rerank_pool": getattr(args, "rerank_pool", 50),
        },
        "index": {
            "seconds": round(index_seconds, 3),
            "db_path": str(db_path),
            "indexed_db_files": indexed_db_files,
            "chunks": chunks,
            "indexed_corpus": indexed_corpus,
            "source_doc_corpus": source_doc_corpus,
        },
        "summary": {
            "rg_context_tokens": rg_context,
            "mgrep_context_tokens": mgrep_context,
            "context_token_reduction_x": safe_ratio(rg_context, mgrep_context),
            "rg_estimated_total_tokens": rg_total,
            "mgrep_estimated_total_tokens": mgrep_total,
            "estimated_total_token_reduction_x": safe_ratio(rg_total, mgrep_total),
            "rg_hit_rate": f"{sum(1 for r in rows if r['rg']['hit'])}/{len(rows)}",
            "mgrep_hit_rate": f"{sum(1 for r in rows if r['mgrep']['hit'])}/{len(rows)}",
            "rg_avg_latency_seconds": round(
                sum(float(r["rg"]["latency_seconds"]) for r in rows) / len(rows), 3
            ),
            "mgrep_avg_latency_seconds": round(
                sum(float(r["mgrep"]["latency_seconds"]) for r in rows) / len(rows), 3
            ),
            "rg_tool_calls": sum(int(r["rg"]["tool_calls"]) for r in rows),
            "mgrep_tool_calls": sum(int(r["mgrep"]["tool_calls"]) for r in rows),
        },
        "tasks": rows,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark real-ripgrep agent context vs local-mgrep top-k."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--db-path")
    parser.add_argument("--tasks", type=Path, help="Optional JSON task list")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--chars-per-token", type=int, default=4)
    parser.add_argument("--rg-max-terms", type=int, default=8)
    parser.add_argument("--rg-max-matches-per-term", type=int, default=20)
    parser.add_argument("--rg-context-lines", type=int, default=2)
    parser.add_argument("--fixed-prompt-tokens", type=int, default=1000)
    parser.add_argument("--final-answer-tokens", type=int, default=300)
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Only print definition, parameters, index, and summary",
    )
    parser.add_argument(
        "--rerank",
        dest="rerank",
        action="store_true",
        default=True,
        help="Apply cross-encoder rerank as second stage (default on)",
    )
    parser.add_argument(
        "--no-rerank",
        dest="rerank",
        action="store_false",
        help="Disable cross-encoder rerank (cosine + lexical only)",
    )
    parser.add_argument("--rerank-pool", type=int, default=50, help="Candidate pool size before reranking")
    parser.add_argument(
        "--reuse-index",
        action="store_true",
        help="Reuse the existing DB at --db-path instead of rebuilding (skips re-embedding)",
    )
    parser.add_argument(
        "--hyde",
        dest="hyde",
        action="store_true",
        default=False,
        help="HyDE: generate a hypothetical code answer with the local LLM and embed that instead of the raw question",
    )
    parser.add_argument(
        "--no-hyde",
        dest="hyde",
        action="store_false",
        help="Disable HyDE (default off)",
    )
    parser.add_argument(
        "--multi-resolution",
        dest="multi_resolution",
        action="store_true",
        default=True,
        help="Two-stage retrieval: file-level cosine top-N then chunk-level (default on)",
    )
    parser.add_argument(
        "--no-multi-resolution",
        dest="multi_resolution",
        action="store_false",
        help="Disable two-stage retrieval; chunk-level cosine over the whole index",
    )
    parser.add_argument("--file-top", dest="file_top", type=int, default=30, help="Number of files surfaced by the file-level stage")
    parser.add_argument("--daemon-url", dest="daemon_url", default=None, help="If set, route every mgrep search through a running mgrep daemon at this URL (skips per-query reranker cold load)")
    parser.add_argument(
        "--lexical-prefilter",
        dest="lexical_prefilter",
        action="store_true",
        default=True,
        help="Use ripgrep to narrow the candidate file set before cosine + rerank (default on)",
    )
    parser.add_argument(
        "--no-lexical-prefilter",
        dest="lexical_prefilter",
        action="store_false",
        help="Disable the ripgrep prefilter; cosine over the full corpus",
    )
    parser.add_argument("--lexical-min-candidates", dest="lexical_min_candidates", type=int, default=2, help="Fall back to corpus-wide cosine when ripgrep returns fewer than this many candidate files")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = benchmark(args)
    if args.summary_only:
        report = {
            key: report[key]
            for key in ("definition", "tooling", "parameters", "index", "summary")
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
