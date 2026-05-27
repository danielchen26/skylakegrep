"""Tests for closed-loop benchmark regression gate."""

from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path


_GATE_PATH = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "closed_loop_regression_gate.py"
)
_SPEC = importlib.util.spec_from_file_location("closed_loop_regression_gate", _GATE_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
_gate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _gate
_SPEC.loader.exec_module(_gate)


def _args(**overrides):
    values = {
        "min_skygrep_path_coverage": 90.0,
        "min_skygrep_evidence_coverage": 90.0,
        "min_skygrep_sufficiency": 85.0,
        "min_context_reduction": 2.0,
        "min_estimated_agent_elapsed_reduction": 1.0,
        "min_work_quality_per_minute_ratio": 1.0,
        "max_completed_task_regression": 5,
    }
    values.update(overrides)
    return Namespace(**values)


def _report(path_coverage: float = 94.7, context_reduction: float = 470.0):
    return {
        "aggregate": {
            "totals": {
                "skygrep-first": {
                    "path_coverage_pct": path_coverage,
                    "evidence_coverage_pct": 99.1,
                    "sufficiency_pct": 96.5,
                    "completed_tasks": 35,
                },
                "rg-only": {
                    "path_coverage_pct": 100.0,
                    "evidence_coverage_pct": 99.3,
                    "sufficiency_pct": 99.7,
                    "completed_tasks": 38,
                },
            },
            "comparison": {
                "context_token_reduction_x": context_reduction,
                "estimated_agent_elapsed_ratio_rg_over_skygrep": 23.71,
                "work_quality_per_minute_ratio_skygrep_over_rg": 22.87,
            },
        }
    }


def test_closed_loop_gate_accepts_current_release_shape():
    result = _gate.evaluate(_report(), _args())
    assert result["ok"], result


def test_closed_loop_gate_accepts_direct_aggregate_policy_shape():
    report = _report()
    aggregate = report["aggregate"]
    report["aggregate"] = {
        "skygrep-first": aggregate["totals"]["skygrep-first"],
        "rg-only": aggregate["totals"]["rg-only"],
        "comparison": aggregate["comparison"],
    }
    result = _gate.evaluate(report, _args())
    assert result["ok"], result


def test_closed_loop_gate_rejects_path_coverage_regression():
    result = _gate.evaluate(_report(path_coverage=50.0), _args())
    assert not result["ok"]
    assert "path_coverage_pct" in result["failures"][0]


def test_closed_loop_gate_rejects_context_economy_regression():
    result = _gate.evaluate(_report(context_reduction=1.1), _args())
    assert not result["ok"]
    assert any("context_token_reduction_x" in failure for failure in result["failures"])
