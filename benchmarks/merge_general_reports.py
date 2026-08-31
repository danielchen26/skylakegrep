# SPDX-License-Identifier: Apache-2.0
"""Merge per-repository General Benchmark v2 receipts without rerunning retrieval."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.general_stats import paired_efficiency
from benchmarks.public_fixtures import (
    GENERAL_MIN_QUALITY_ELIGIBLE_TASKS,
    GENERAL_MIN_REPOS,
)
from benchmarks.universal_closed_loop_benchmark import (
    PUBLIC_REPOS,
    _attach_source_gate,
    _summarize_rows,
)


CONSISTENT_PARAMETER_KEYS = (
    "tokenizer",
    "trials",
    "tokens_per_second",
    "sufficient_threshold",
    "timeout_seconds",
    "allow_root_fallback",
    "quality_floor",
    "noninferiority_margin_pct",
    "bootstrap_samples",
    "bootstrap_seed",
    "min_general_tasks",
    "min_general_repos",
)


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 2:
        raise ValueError(f"{path}: expected schema_version 2")
    if not payload.get("rows"):
        raise ValueError(f"{path}: per-repository receipt must contain compact rows")
    return payload


def merge_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not reports:
        raise ValueError("at least one report is required")

    first_parameters = reports[0]["parameters"]
    policies = reports[0]["definition"]["policies"]
    mode = reports[0]["definition"]["mode"]
    trials = int(first_parameters["trials"])
    if policies != ["skygrep-first", "rg-only"]:
        raise ValueError("general receipts require skygrep-first and rg-only policies")
    if mode not in {"adaptive-only", "full-matrix low/medium/high"}:
        raise ValueError(f"unsupported general benchmark mode: {mode}")
    if trials < 1:
        raise ValueError("general receipts require at least one trial")
    sections: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    repo_keys: list[str] = []
    benchmark_source_commits: set[str] = set()
    observed_task_trials: set[tuple[str, str, str, str, int]] = set()
    observed_pair_members: dict[tuple[str, str, str, int], set[str]] = {}

    for report in reports:
        parameters = report["parameters"]
        for key in CONSISTENT_PARAMETER_KEYS:
            if parameters.get(key) != first_parameters.get(key):
                raise ValueError(f"all receipts must use the same {key}")
        if report["definition"].get("policies") != policies:
            raise ValueError("all receipts must use the same policies")
        if report["definition"].get("mode") != mode:
            raise ValueError("all receipts must use the same benchmark mode")
        environment = report.get("environment", {})
        source_commit = str(environment.get("benchmark_source_commit", "unknown"))
        tracked_clean = str(
            environment.get("benchmark_source_tracked_clean", "false")
        ).lower()
        if len(source_commit) != 40 or any(
            character not in "0123456789abcdef" for character in source_commit.lower()
        ):
            raise ValueError("all receipts must identify a full benchmark source commit")
        if tracked_clean != "true":
            raise ValueError("all receipts must come from a clean tracked benchmark worktree")
        benchmark_source_commits.add(source_commit)
        report_sections = report.get("sections", [])
        if len(report_sections) != 1 or report_sections[0].get("skipped"):
            raise ValueError("each input must contain one completed repository section")
        repo = str(report_sections[0]["repo"])
        if repo in repo_keys:
            raise ValueError(f"duplicate repository receipt: {repo}")
        if repo not in PUBLIC_REPOS:
            raise ValueError(f"unexpected public repository receipt: {repo}")
        spec = PUBLIC_REPOS[repo]
        if report_sections[0].get("commit") != spec.commit:
            raise ValueError(f"{repo}: receipt commit does not match the public pin")
        if int(report_sections[0].get("tasks", 0)) != len(spec.tasks):
            raise ValueError(f"{repo}: receipt task count does not match the public fixture")
        if parameters.get("repos") != [repo]:
            raise ValueError(f"{repo}: receipt must contain exactly its own repository parameter")
        if parameters.get("max_tasks_per_repo") is not None:
            raise ValueError(f"{repo}: partial task receipts cannot enter a general claim")
        repo_keys.append(repo)
        sections.extend(report_sections)
        expected_task_ids = {str(task["id"]) for task in spec.tasks}
        observed_task_rows: dict[str, int] = {task_id: 0 for task_id in expected_task_ids}
        for row in report["rows"]:
            if str(row.get("repo")) != repo:
                raise ValueError(f"{repo}: row belongs to a different repository")
            task_id = str(row.get("task_id"))
            if task_id not in expected_task_ids:
                raise ValueError(f"{repo}: unexpected task row {task_id}")
            effort = str(row.get("effort"))
            policy = str(row.get("policy"))
            trial = int(row.get("trial", 1))
            if effort not in {"low", "medium", "high"}:
                raise ValueError(f"{repo}: unexpected effort {effort!r}")
            if policy not in policies:
                raise ValueError(f"{repo}: unexpected policy {policy!r}")
            if not 1 <= trial <= trials:
                raise ValueError(f"{repo}: unexpected trial {trial}")
            key = (
                str(row.get("repo")),
                task_id,
                effort,
                policy,
                trial,
            )
            if key in observed_task_trials:
                raise ValueError(f"duplicate paired observation: {key}")
            observed_task_trials.add(key)
            pair_key = (repo, task_id, effort, trial)
            observed_pair_members.setdefault(pair_key, set()).add(policy)
            observed_task_rows[task_id] += 1
            rows.append(row)
        efforts_per_task = 3 if mode == "full-matrix low/medium/high" else 1
        expected_rows_per_task = len(policies) * trials * efforts_per_task
        incomplete = sorted(
            task_id
            for task_id, count in observed_task_rows.items()
            if count != expected_rows_per_task
        )
        if incomplete:
            raise ValueError(
                f"{repo}: incomplete task observations for {incomplete[:5]} "
                f"(expected {expected_rows_per_task} rows per task)"
            )
        incomplete_pairs = sorted(
            pair_key
            for pair_key, pair_policies in observed_pair_members.items()
            if pair_key[0] == repo and pair_policies != set(policies)
        )
        if incomplete_pairs:
            raise ValueError(f"{repo}: unpaired policy observations for {incomplete_pairs[:5]}")

    expected_repos = set(PUBLIC_REPOS)
    observed_repos = set(repo_keys)
    if observed_repos != expected_repos:
        missing = sorted(expected_repos - observed_repos)
        unexpected = sorted(observed_repos - expected_repos)
        raise ValueError(
            "public receipt set is incomplete or unexpected: "
            f"missing={missing}, unexpected={unexpected}"
        )
    if len(benchmark_source_commits) != 1:
        raise ValueError("all receipts must use the same benchmark source commit")

    tokens_per_second = float(first_parameters["tokens_per_second"])
    sufficient_threshold = float(first_parameters["sufficient_threshold"])
    quality_floor = float(first_parameters["quality_floor"])
    noninferiority_margin = float(first_parameters["noninferiority_margin_pct"])
    bootstrap_samples = int(first_parameters["bootstrap_samples"])
    bootstrap_seed = int(first_parameters["bootstrap_seed"])
    # The gate is the protocol's, never the inputs'. A per-repository receipt
    # must relax these to run at all — one repository cannot clear a
    # three-repository minimum — so reading them back out of the first receipt
    # published a six-repository claim gated at one repository, which is what
    # the 2026-08-31 merge did. The relaxed values the inputs used are recorded
    # rather than dropped, so the weaker per-run gate stays auditable.
    min_tasks = GENERAL_MIN_QUALITY_ELIGIBLE_TASKS
    min_repos = GENERAL_MIN_REPOS
    per_receipt_minimums = {
        "min_general_tasks": int(first_parameters["min_general_tasks"]),
        "min_general_repos": int(first_parameters["min_general_repos"]),
    }

    merged_parameters = dict(first_parameters)
    merged_parameters.update(
        {
            "repos": sorted(repo_keys),
            "oss_root": "<oss-root>",
            "min_general_tasks": min_tasks,
            "min_general_repos": min_repos,
            "per_receipt_gate_minimums": per_receipt_minimums,
            "benchmark_wall_seconds": None,
            "parallel_repo_wall_seconds": {
                str(report["sections"][0]["repo"]): report["parameters"].get(
                    "benchmark_wall_seconds"
                )
                for report in reports
            },
        }
    )
    source_commit = next(iter(benchmark_source_commits))
    merged_environment = {
        "benchmark_source_commit": source_commit,
        "benchmark_source_tracked_clean": "true",
        "repo_jobs": {
            str(report["sections"][0]["repo"]): report.get("environment", {})
            for report in reports
        },
    }
    generalization = _attach_source_gate(
        paired_efficiency(
            rows,
            quality_floor=quality_floor,
            noninferiority_margin_pct=noninferiority_margin,
            bootstrap_samples=bootstrap_samples,
            bootstrap_seed=bootstrap_seed,
            min_pairs=min_tasks,
            min_repos=min_repos,
        ),
        merged_environment,
    )
    return {
        "schema_version": 2,
        "definition": {
            **reports[0]["definition"],
            "benchmark": "General Benchmark v2: merged parallel public-repository receipts",
            "merge_note": "Each repository ran independently; paired statistics were recomputed over all compact rows.",
        },
        "parameters": merged_parameters,
        "environment": merged_environment,
        "aggregate": _summarize_rows(rows, policies, tokens_per_second, sufficient_threshold),
        "generalization": generalization,
        "sections": sorted(sections, key=lambda section: str(section["repo"])),
        "rows": [],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    merged = merge_reports([_load(path) for path in args.reports])
    rendered = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=args.output.parent,
        prefix=f".{args.output.name}.",
        delete=False,
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    temporary.replace(args.output)
    print(rendered, end="")


if __name__ == "__main__":
    main()
