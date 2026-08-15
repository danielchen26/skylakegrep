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
        "require_general_reportable": False,
        "min_general_eligible_fraction": 0.8,
        "min_general_median_context_reduction": 1.0,
        "min_general_context_ci_low": 1.0,
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


def test_closed_loop_gate_accepts_quality_gated_general_report():
    report = _report()
    report["generalization"] = {
        "claim_status": "reportable",
        "quality_eligible_fraction": 0.9,
        "quality_gate": {"passed": True},
        "sample_gate": {"passed": True},
        "source_gate": {"passed": True},
        "paired_distributions": {"context_token_reduction_x": {"median": 2.5}},
        "context_token_median_95pct_ci": {"low": 1.8, "high": 3.1},
    }
    result = _gate.evaluate(report, _args(require_general_reportable=True))
    assert result["ok"], result


def test_closed_loop_gate_rejects_general_multiplier_without_quality():
    report = _report()
    report["generalization"] = {
        "claim_status": "insufficient",
        "quality_eligible_fraction": 0.2,
        "quality_gate": {"passed": False},
        "sample_gate": {"passed": True},
        "source_gate": {"passed": True},
        "paired_distributions": {"context_token_reduction_x": {"median": 20.0}},
        "context_token_median_95pct_ci": {"low": 10.0, "high": 30.0},
    }
    result = _gate.evaluate(report, _args(require_general_reportable=True))
    assert not result["ok"]
    assert any("claim_status" in failure for failure in result["failures"])
    assert any("quality noninferiority" in failure for failure in result["failures"])


def test_closed_loop_gate_rejects_general_report_without_clean_source():
    report = _report()
    report["generalization"] = {
        "claim_status": "reportable",
        "quality_eligible_fraction": 0.9,
        "quality_gate": {"passed": True},
        "sample_gate": {"passed": True},
        "source_gate": {"passed": False},
        "paired_distributions": {"context_token_reduction_x": {"median": 2.5}},
        "context_token_median_95pct_ci": {"low": 1.8, "high": 3.1},
    }
    result = _gate.evaluate(report, _args(require_general_reportable=True))
    assert not result["ok"]
    assert any("source gate" in failure for failure in result["failures"])


def test_closed_loop_gate_only_enforces_general_contract_when_requested():
    report = _report()
    report["generalization"] = {"claim_status": "insufficient"}
    result = _gate.evaluate(report, _args(require_general_reportable=False))
    assert result["ok"], result
