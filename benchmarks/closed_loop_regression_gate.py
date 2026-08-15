"""Regression gate for closed-loop agent benchmark reports.

The universal benchmark is intentionally too broad for every CI run, but its
output should still be machine-checkable. This gate reads a saved benchmark
JSON report and fails if the skygrep-first policy no longer meets minimum
coverage, sufficiency, context-economy, and closed-loop utility targets.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _load_report(path: str | None) -> dict[str, Any]:
    raw = sys.stdin.read() if not path or path == "-" else Path(path).read_text()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("benchmark report must be a JSON object")
    return payload


def _aggregate(report: dict[str, Any]) -> dict[str, Any]:
    if "aggregate" in report and isinstance(report["aggregate"], dict):
        return report["aggregate"]
    return report


def _policy_totals(aggregate: dict[str, Any]) -> dict[str, Any]:
    """Return policy totals from either supported benchmark report shape."""

    totals = aggregate.get("totals")
    if isinstance(totals, dict):
        return totals
    return aggregate


def _metric(obj: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = obj.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate(report: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    aggregate = _aggregate(report)
    totals = _policy_totals(aggregate)
    comparison = aggregate.get("comparison", {})
    sky = totals.get("skygrep-first", {})
    rg = totals.get("rg-only", {})
    failures: list[str] = []

    checks = {
        "path_coverage_pct": (
            _metric(sky, "path_coverage_pct"),
            args.min_skygrep_path_coverage,
        ),
        "evidence_coverage_pct": (
            _metric(sky, "evidence_coverage_pct"),
            args.min_skygrep_evidence_coverage,
        ),
        "sufficiency_pct": (
            _metric(sky, "sufficiency_pct"),
            args.min_skygrep_sufficiency,
        ),
        "context_token_reduction_x": (
            _metric(comparison, "context_token_reduction_x"),
            args.min_context_reduction,
        ),
        "estimated_agent_elapsed_ratio_rg_over_skygrep": (
            _metric(comparison, "estimated_agent_elapsed_ratio_rg_over_skygrep"),
            args.min_estimated_agent_elapsed_reduction,
        ),
        "work_quality_per_minute_ratio_skygrep_over_rg": (
            _metric(comparison, "work_quality_per_minute_ratio_skygrep_over_rg"),
            args.min_work_quality_per_minute_ratio,
        ),
    }
    for key, (actual, minimum) in checks.items():
        if actual < minimum:
            failures.append(f"{key}={actual} below required {minimum}")

    sky_completed = int(_metric(sky, "completed_tasks"))
    rg_completed = int(_metric(rg, "completed_tasks"))
    max_regression = int(args.max_completed_task_regression)
    if rg_completed - sky_completed > max_regression:
        failures.append(
            "completed task regression "
            f"{rg_completed - sky_completed} exceeds allowed {max_regression}"
        )

    general = report.get("generalization", {})
    require_general = bool(getattr(args, "require_general_reportable", False))
    if require_general:
        if general.get("claim_status") != "reportable":
            failures.append(
                f"general claim_status={general.get('claim_status', 'missing')} is not reportable"
            )
        quality_gate = general.get("quality_gate", {})
        if not quality_gate.get("passed", False):
            failures.append("general quality noninferiority gate did not pass")
        sample_gate = general.get("sample_gate", {})
        if not sample_gate.get("passed", False):
            failures.append("general minimum sample gate did not pass")
        source_gate = general.get("source_gate", {})
        if not source_gate.get("passed", False):
            failures.append("general benchmark source gate did not pass")
        eligible_fraction = _metric(general, "quality_eligible_fraction")
        minimum_eligible = float(getattr(args, "min_general_eligible_fraction", 0.8))
        if eligible_fraction < minimum_eligible:
            failures.append(
                f"general quality_eligible_fraction={eligible_fraction} below required {minimum_eligible}"
            )
        context_distribution = general.get("paired_distributions", {}).get(
            "context_token_reduction_x", {}
        )
        median_reduction = _metric(context_distribution, "median")
        minimum_median = float(getattr(args, "min_general_median_context_reduction", 1.0))
        if median_reduction < minimum_median:
            failures.append(
                f"general median context reduction={median_reduction} below required {minimum_median}"
            )
        ci_low = _metric(general.get("context_token_median_95pct_ci", {}), "low")
        minimum_ci_low = float(getattr(args, "min_general_context_ci_low", 1.0))
        if ci_low < minimum_ci_low:
            failures.append(
                f"general context reduction CI low={ci_low} below required {minimum_ci_low}"
            )

    return {
        "ok": not failures,
        "failures": failures,
        "skygrep_first": sky,
        "rg_only": rg,
        "comparison": comparison,
        "thresholds": {
            "min_skygrep_path_coverage": args.min_skygrep_path_coverage,
            "min_skygrep_evidence_coverage": args.min_skygrep_evidence_coverage,
            "min_skygrep_sufficiency": args.min_skygrep_sufficiency,
            "min_context_reduction": args.min_context_reduction,
            "min_estimated_agent_elapsed_reduction": args.min_estimated_agent_elapsed_reduction,
            "min_work_quality_per_minute_ratio": args.min_work_quality_per_minute_ratio,
            "max_completed_task_regression": max_regression,
            "require_general_reportable": require_general,
            "min_general_eligible_fraction": float(
                getattr(args, "min_general_eligible_fraction", 0.8)
            ),
            "min_general_median_context_reduction": float(
                getattr(args, "min_general_median_context_reduction", 1.0)
            ),
            "min_general_context_ci_low": float(
                getattr(args, "min_general_context_ci_low", 1.0)
            ),
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check a closed-loop benchmark JSON report.")
    parser.add_argument("report", nargs="?", help="Benchmark JSON path, or stdin when omitted / '-'.")
    parser.add_argument("--min-skygrep-path-coverage", type=float, default=90.0)
    parser.add_argument("--min-skygrep-evidence-coverage", type=float, default=90.0)
    parser.add_argument("--min-skygrep-sufficiency", type=float, default=85.0)
    parser.add_argument("--min-context-reduction", type=float, default=2.0)
    parser.add_argument("--min-estimated-agent-elapsed-reduction", type=float, default=1.0)
    parser.add_argument("--min-work-quality-per-minute-ratio", type=float, default=1.0)
    parser.add_argument("--max-completed-task-regression", type=int, default=5)
    parser.add_argument("--require-general-reportable", action="store_true")
    parser.add_argument("--min-general-eligible-fraction", type=float, default=0.8)
    parser.add_argument("--min-general-median-context-reduction", type=float, default=1.0)
    parser.add_argument("--min-general-context-ci-low", type=float, default=1.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    result = evaluate(_load_report(args.report), args)
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
