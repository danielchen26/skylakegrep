# SPDX-License-Identifier: Apache-2.0
"""Cross-project closed-loop benchmark for agent search workflows.

This runner measures whether a disciplined LLM agent gets enough context to
finish a task when it uses skygrep adaptively versus a raw-ripgrep baseline.
It is intentionally broader than the single-repo release benchmark:

* the optional current-project fixture covers snippet, deep, and abstract
  evidence workflows;
* the default pinned public matrix covers source-evidence tasks across Go,
  Python, JavaScript/Flow, Java, Rust, and TypeScript projects;
* adaptive-only mode measures the depth a real agent should choose, instead of
  mechanically running low/medium/high for every task.

The benchmark still cannot prove all future projects will behave identically.
It gives a reproducible, multi-repo signal with explicit scope and metrics.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import platform
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.agent_tool_depth_benchmark import DEFAULT_TASKS
from benchmarks.closed_loop_agent_benchmark import (
    _adaptive_effort_for_task,
    _aggregate,
    _closed_loop,
    _compare_totals,
)
from benchmarks.general_stats import paired_efficiency
from benchmarks.public_fixtures import (
    RepoSpec,
    git_commit,
    load_registry,
    prepare_repo,
    validate_repo_fixture,
)
from benchmarks.token_savings import tokenizer_metadata
from skylakegrep.src.config import project_db_path
from skylakegrep.src.indexer import index_batch_size


PUBLIC_REPOS = load_registry()


def _run(cmd: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, timeout=timeout)


def _git_commit(repo: Path) -> str:
    return git_commit(repo)


def _public_environment() -> dict[str, str]:
    try:
        package_version = importlib.metadata.version("skylakegrep")
    except importlib.metadata.PackageNotFoundError:
        package_version = "source-tree"
    tracked_clean = all(
        subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            timeout=10,
        ).returncode
        == 0
        for command in (
            ["git", "diff", "--quiet", "HEAD", "--"],
            ["git", "diff", "--cached", "--quiet", "HEAD", "--"],
        )
    )
    return {
        "skylakegrep_version": package_version,
        "benchmark_source_commit": _git_commit(PROJECT_ROOT),
        "benchmark_source_tracked_clean": str(tracked_clean).lower(),
        "python_version": platform.python_version(),
        "system": platform.system(),
        "machine": platform.machine(),
        "embedding_model": os.environ.get("OLLAMA_EMBED_MODEL", "bge-m3"),
        "index_batch_size": str(index_batch_size()),
    }


def _repo_has_fixture_files(repo: Path, spec: RepoSpec) -> bool:
    for item in spec.tasks[:3]:
        candidates = [item["expected"], *item.get("expected_alternatives", [])]
        if any((repo / str(candidate)).exists() for candidate in candidates):
            return True
    return False


def _ensure_repo(spec: RepoSpec, oss_root: Path, prepare: bool, timeout: float) -> Path | None:
    repo = oss_root / spec.subdir
    if prepare:
        return prepare_repo(spec, oss_root, timeout=timeout)
    if repo.exists() and _repo_has_fixture_files(repo, spec):
        failures = validate_repo_fixture(repo, spec)
        if not failures:
            return repo
        print(f"warning: {spec.key} fixture validation failed: {'; '.join(failures[:5])}", file=sys.stderr)
        return None
    if repo.exists():
        print(
            f"warning: {repo} exists but does not contain the public fixture paths; "
            "use a fresh --oss-root with --prepare",
            file=sys.stderr,
        )
        return None
    if not prepare:
        return None
    return None


def _public_task(item: dict[str, Any]) -> dict[str, Any]:
    alternatives = list(item.get("expected_alternatives", []))
    expected = str(item["expected"])
    return {
        "id": str(item["id"]),
        "difficulty": str(item["difficulty"]),
        "abstract_level": str(item["abstract_level"]),
        "deliverable": str(item["deliverable"]),
        "query": str(item["question"]),
        "expected_paths": [expected],
        "expected_path_groups": [[expected, *alternatives]],
        "evidence_terms": list(item["evidence_terms"]),
        "quality_terms": list(item["quality_terms"]),
        "min_paths": int(item.get("min_paths", 1)),
    }


def _project_tasks(limit: int | None) -> list[dict[str, Any]]:
    tasks = list(DEFAULT_TASKS)
    if limit is not None:
        tasks = tasks[:limit]
    return tasks


def _public_tasks(spec: RepoSpec, limit: int | None) -> list[dict[str, Any]]:
    tasks = [_public_task(item) for item in spec.tasks]
    if limit is not None:
        tasks = tasks[:limit]
    return tasks


@contextmanager
def _project_db_scope(root: Path):
    """Force every subprocess for one fixture to use its project-scoped DB."""

    previous = os.environ.get("SKYGREP_DB_PATH")
    db_path = project_db_path(root)
    os.environ["SKYGREP_DB_PATH"] = str(db_path)
    try:
        yield db_path
    finally:
        if previous is None:
            os.environ.pop("SKYGREP_DB_PATH", None)
        else:
            os.environ["SKYGREP_DB_PATH"] = previous


def _index_snapshot(db_path: Path) -> dict[str, Any]:
    if not db_path.is_file():
        raise RuntimeError("project-scoped benchmark index is missing; rerun with --refresh-index")
    with sqlite3.connect(db_path) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        chunks, files = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT file) FROM chunks"
        ).fetchone()
    if integrity != "ok":
        raise RuntimeError(f"project-scoped benchmark index failed integrity_check: {integrity}")
    if int(chunks) <= 0 or int(files) <= 0:
        raise RuntimeError("project-scoped benchmark index is empty; rerun with --refresh-index")
    return {
        "database_scope": "project-derived",
        "integrity": integrity,
        "chunks": int(chunks),
        "files": int(files),
    }


def _refresh_index(
    root: Path,
    db_path: Path,
    *,
    reset: bool,
    timeout: float,
) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "skylakegrep.src.cli", "index"]
    cmd.append("--reset" if reset else "--incremental")
    cmd.append(str(root))
    started = time.perf_counter()
    proc = _run(cmd, root, timeout=timeout)
    elapsed = time.perf_counter() - started
    if proc.returncode != 0:
        raise RuntimeError(f"indexing failed for {root.name}: {(proc.stderr or proc.stdout)[-500:]}")
    return {
        "refreshed": True,
        "reset": reset,
        "elapsed_seconds": round(elapsed, 3),
        **_index_snapshot(db_path),
    }


def _run_policy_rows(
    repo_key: str,
    root: Path,
    tasks: list[dict[str, Any]],
    *,
    policies: list[str],
    timeout: float,
    sufficient_threshold: float,
    allow_root_fallback: bool,
    full_matrix: bool,
    trials: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trial in range(1, max(1, trials) + 1):
        for task in tasks:
            efforts = ["low", "medium", "high"] if full_matrix else [_adaptive_effort_for_task(task)]
            for effort in efforts:
                for policy in policies:
                    row = _closed_loop(
                        root,
                        task,
                        effort,
                        policy=policy,
                        timeout=timeout,
                        sufficient_threshold=sufficient_threshold,
                        allow_root_fallback=allow_root_fallback,
                    )
                    row["trial"] = trial
                    row["repo"] = repo_key
                    rows.append(row)
    return rows


def _summarize_rows(
    rows: list[dict[str, Any]],
    policies: list[str],
    tokens_per_second: float,
    threshold: float,
) -> dict[str, Any]:
    totals = {
        policy: _aggregate([row for row in rows if row["policy"] == policy], tokens_per_second, threshold)
        for policy in policies
    }
    comparison: dict[str, Any] = {}
    if "skygrep-first" in totals and "rg-only" in totals and totals["skygrep-first"] and totals["rg-only"]:
        comparison = _compare_totals(totals["skygrep-first"], totals["rg-only"])
    return {"totals": totals, "comparison": comparison}


def _compact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keep = {
        "repo",
        "policy",
        "effort",
        "task_id",
        "difficulty",
        "abstract_level",
        "stop_reason",
        "tool_calls",
        "elapsed_seconds",
        "tool_elapsed_seconds",
        "context_tokens",
        "returned_paths",
        "path_coverage",
        "path_precision",
        "rank_first_hit",
        "reciprocal_rank",
        "hit_at_1",
        "hit_at_3",
        "evidence_coverage",
        "sufficiency",
        "task_completion_quality",
        "work_completed",
        "missing_paths",
        "missing_evidence_terms",
        "steps",
        "trial",
    }
    return [{key: row[key] for key in keep if key in row} for row in rows]


def _attach_source_gate(
    generalization: dict[str, Any],
    environment: dict[str, Any],
) -> dict[str, Any]:
    """Require a clean, identifiable benchmark source before publishing a claim."""

    commit = str(environment.get("benchmark_source_commit", "unknown"))
    tracked_clean = (
        str(environment.get("benchmark_source_tracked_clean", "false")).lower()
        == "true"
    )
    identifiable_commit = len(commit) == 40 and all(
        character in "0123456789abcdef" for character in commit.lower()
    )
    passed = identifiable_commit and tracked_clean
    generalization["source_gate"] = {
        "passed": passed,
        "benchmark_source_commit": commit,
        "tracked_worktree_clean": tracked_clean,
    }
    if not passed:
        generalization["claim_status"] = "insufficient"
    return generalization


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    policies = args.policy or ["skygrep-first", "rg-only"]
    sections: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []

    if "self" in args.repo:
        tasks = _project_tasks(args.max_tasks_per_repo)
        with _project_db_scope(PROJECT_ROOT) as db_path:
            index_receipt = (
                _refresh_index(
                    PROJECT_ROOT,
                    db_path,
                    reset=getattr(args, "reset_index", False),
                    timeout=getattr(args, "index_timeout", 7200.0),
                )
                if getattr(args, "refresh_index", False)
                else {"refreshed": False, **_index_snapshot(db_path)}
            )
            rows = _run_policy_rows(
                "self",
                PROJECT_ROOT,
                tasks,
                policies=policies,
                timeout=args.timeout,
                sufficient_threshold=args.sufficient_threshold,
                allow_root_fallback=args.allow_root_fallback,
                full_matrix=args.full_matrix,
                trials=args.trials,
            )
        all_rows.extend(rows)
        section = {
            "repo": "self",
            "label": "skylakegrep (Python CLI + docs)",
            "root": "<project-root>",
            "commit": _git_commit(PROJECT_ROOT),
            "index": index_receipt,
            "tasks": len(tasks),
            **_summarize_rows(rows, policies, args.tokens_per_second, args.sufficient_threshold),
        }
        sections.append(section)

    for key in [repo for repo in args.repo if repo != "self"]:
        spec = PUBLIC_REPOS[key]
        repo = _ensure_repo(spec, args.oss_root, args.prepare, args.clone_timeout)
        if repo is None:
            sections.append(
                {
                    "repo": key,
                    "label": spec.label,
                    "skipped": True,
                    "reason": f"repo not found at {args.oss_root / spec.subdir}; rerun with --prepare to clone",
                }
            )
            continue
        tasks = _public_tasks(spec, args.max_tasks_per_repo)
        with _project_db_scope(repo) as db_path:
            index_receipt = (
                _refresh_index(
                    repo,
                    db_path,
                    reset=getattr(args, "reset_index", False),
                    timeout=getattr(args, "index_timeout", 7200.0),
                )
                if getattr(args, "refresh_index", False)
                else {"refreshed": False, **_index_snapshot(db_path)}
            )
            rows = _run_policy_rows(
                key,
                repo,
                tasks,
                policies=policies,
                timeout=args.timeout,
                sufficient_threshold=args.sufficient_threshold,
                allow_root_fallback=args.allow_root_fallback,
                full_matrix=args.full_matrix,
                trials=args.trials,
            )
        all_rows.extend(rows)
        sections.append(
            {
                "repo": key,
                "label": spec.label,
                "root": f"<oss-root>/{spec.subdir}",
                "commit": _git_commit(repo),
                "index": index_receipt,
                "tasks": len(tasks),
                **_summarize_rows(rows, policies, args.tokens_per_second, args.sufficient_threshold),
            }
        )

    aggregate = _summarize_rows(all_rows, policies, args.tokens_per_second, args.sufficient_threshold)
    environment = _public_environment()
    generalization = _attach_source_gate(
        paired_efficiency(
            all_rows,
            quality_floor=getattr(args, "quality_floor", 0.85),
            noninferiority_margin_pct=getattr(args, "noninferiority_margin_pct", 2.0),
            bootstrap_samples=getattr(args, "bootstrap_samples", 2000),
            bootstrap_seed=getattr(args, "bootstrap_seed", 20260814),
            min_pairs=getattr(args, "min_general_tasks", 30),
            min_repos=getattr(args, "min_general_repos", 3),
        ),
        environment,
    )
    return {
        "schema_version": 2,
        "definition": {
            "benchmark": "General Benchmark v2: universal closed-loop retrieval workflow",
            "scope": (
                "multi-repo adaptive workflow benchmark; strong generalization signal, "
                "not a proof over all future projects"
            ),
            "task_mix": (
                "self repo has locate/snippet/deep/abstract tasks; public OSS repos require "
                "path and literal source evidence across Python, JavaScript/Flow, Rust, Go, "
                "Java, and TypeScript"
            ),
            "trust_model": (
                "credible when the fixture repos, commits, task count, trials, and failure rows "
                "are reported; the benchmark is designed to expose misses rather than force a win"
            ),
            "policies": policies,
            "mode": "full-matrix low/medium/high" if args.full_matrix else "adaptive-only",
            "privacy": "public OSS tasks use public repository paths only; absolute roots are redacted",
            "headline_rule": (
                "efficiency is reportable only on paired tasks where both policies pass the "
                "quality floor and the skygrep policy passes the aggregate noninferiority gate"
            ),
            "quality_note": (
                "task_completion_quality is a deterministic retrieved-context proxy over paths, "
                "literal source facts, noise, sufficiency, and stopping; it is not a graded "
                "model-generated final answer"
            ),
            "precision_note": (
                "path_precision is precision@k and is bounded by relevant/k: with one "
                "relevant file and --top 8 no retriever can exceed 12.5%, so it grades "
                "the chosen top-k more than it grades ranking. Compare tools on mrr, "
                "hit_at_1_pct, hit_at_3_pct and mean_rank_when_found instead — those "
                "measure how many wrong files an agent opens before the right one"
            ),
            "elapsed_note": (
                "elapsed_seconds is measured harness wall time including scoring/token counting; "
                "tool_elapsed_seconds measures retrieval subprocess/file-read time before token "
                "counting; estimated_agent_elapsed_seconds is modelled and never used for the "
                "General Benchmark headline"
            ),
            "token_note": (
                "context tokens cover retrieval tool payloads, not model reasoning or provider "
                "billing tokens"
            ),
        },
        "parameters": {
            "repos": args.repo,
            "oss_root": "<oss-root>",
            "max_tasks_per_repo": args.max_tasks_per_repo,
            "timeout_seconds": args.timeout,
            "sufficient_threshold": args.sufficient_threshold,
            "tokens_per_second": args.tokens_per_second,
            "allow_root_fallback": args.allow_root_fallback,
            "trials": args.trials,
            "tokenizer": tokenizer_metadata(),
            "quality_floor": getattr(args, "quality_floor", 0.85),
            "noninferiority_margin_pct": getattr(args, "noninferiority_margin_pct", 2.0),
            "bootstrap_samples": getattr(args, "bootstrap_samples", 2000),
            "bootstrap_seed": getattr(args, "bootstrap_seed", 20260814),
            "min_general_tasks": getattr(args, "min_general_tasks", 30),
            "min_general_repos": getattr(args, "min_general_repos", 3),
            "benchmark_wall_seconds": round(time.perf_counter() - started, 3),
        },
        "environment": environment,
        "aggregate": aggregate,
        "generalization": generalization,
        "sections": sections,
        "rows": [] if args.summary_only else _compact_rows(all_rows),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Universal closed-loop skygrep benchmark.")
    parser.add_argument(
        "--repo",
        action="append",
        choices=["self", *sorted(PUBLIC_REPOS)],
        default=None,
        help="Repo fixture to run. Repeatable. Defaults to the six public pinned repos; self is explicit opt-in.",
    )
    parser.add_argument("--oss-root", type=Path, default=Path("/tmp/skygrep-general-v2-repos"))
    parser.add_argument("--prepare", action="store_true", help="Clone missing public OSS repos into --oss-root.")
    parser.add_argument("--clone-timeout", type=float, default=600.0)
    parser.add_argument("--max-tasks-per-repo", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--sufficient-threshold", type=float, default=0.85)
    parser.add_argument("--tokens-per-second", type=float, default=30_000.0)
    parser.add_argument("--allow-root-fallback", action="store_true")
    parser.add_argument("--refresh-index", action="store_true")
    parser.add_argument("--reset-index", action="store_true")
    parser.add_argument(
        "--index-timeout",
        type=float,
        default=7200.0,
        help="Per-repository indexing timeout in seconds (default: 7200 for large public repos).",
    )
    parser.add_argument("--full-matrix", action="store_true")
    parser.add_argument("--policy", choices=["skygrep-first", "rg-only"], action="append")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--trials", type=int, default=1, help="Repeat each policy/task/effort combination N times.")
    parser.add_argument("--quality-floor", type=float, default=0.85)
    parser.add_argument("--noninferiority-margin-pct", type=float, default=2.0)
    parser.add_argument("--bootstrap-samples", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=20260814)
    parser.add_argument(
        "--min-general-tasks",
        "--min-general-pairs",
        dest="min_general_tasks",
        type=int,
        default=30,
        help="Minimum unique quality-eligible tasks; repeated trials do not increase this count.",
    )
    parser.add_argument("--min-general-repos", type=int, default=3)
    parser.add_argument("--report", type=Path, help="Write the complete JSON receipt to this path.")
    parser.add_argument(
        "--tokenizer",
        choices=["chars", "auto", "tiktoken"],
        default="chars",
        help="Token counter for tool context. tiktoken is optional; chars is the documented fallback.",
    )
    args = parser.parse_args()
    if args.repo is None:
        args.repo = sorted(PUBLIC_REPOS)
    return args


def main() -> None:
    args = parse_args()
    os.environ["SKYGREP_BENCH_TOKENIZER"] = args.tokenizer
    report = run(args)
    skipped = [section["repo"] for section in report["sections"] if section.get("skipped")]
    if args.report and skipped:
        raise SystemExit(f"refusing to save an incomplete report; skipped repositories: {', '.join(skipped)}")
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.report.with_suffix(args.report.suffix + ".tmp")
        temporary.write_text(rendered + "\n", encoding="utf-8")
        temporary.replace(args.report)
    print(rendered)


if __name__ == "__main__":
    main()
