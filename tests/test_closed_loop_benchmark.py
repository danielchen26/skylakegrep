"""Tests for closed-loop benchmark helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_BENCHMARK_PATH = (
    Path(__file__).resolve().parents[1] / "benchmarks" / "closed_loop_agent_benchmark.py"
)
_SPEC = importlib.util.spec_from_file_location("closed_loop_agent_benchmark", _BENCHMARK_PATH)
assert _SPEC is not None
assert _SPEC.loader is not None
_benchmark = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _benchmark
_SPEC.loader.exec_module(_benchmark)
_path_probe_rank = _benchmark._path_probe_rank
_probe_terms = _benchmark._probe_terms
_completion_quality = _benchmark._completion_quality
_score_context_for_task = _benchmark._score_context_for_task
COMPLETION_CONTRACTS = _benchmark.COMPLETION_CONTRACTS


def test_probe_terms_keep_short_domain_tokens_and_filter_wrappers():
    terms = _probe_terms(
        "Where does Django turn an incoming URL into the view function that should handle it?",
        max_terms=10,
    )

    assert "url" in terms
    assert "view" in terms
    assert "django" in terms
    assert "incoming" not in terms
    assert "should" not in terms
    assert "into" not in terms


def test_path_probe_rank_prefers_source_over_docs_when_term_evidence_ties():
    terms = {"url", "view", "function"}

    source_rank = _path_probe_rank("django/urls/base.py", terms)
    docs_rank = _path_probe_rank("docs/topics/http/urls.txt", terms)

    assert source_rank < docs_rank


def test_path_decision_stops_when_correct_path_is_known_without_body_symbols():
    task = {
        "id": "locate-cli-entrypoint",
        "abstract_level": "locate",
        "expected_paths": ["skylakegrep/src/cli.py"],
        "evidence_terms": ["click.group", "search"],
    }
    payloads = ['[{"path": "skylakegrep/src/cli.py"}]']
    paths = ["skylakegrep/src/cli.py"]

    score = _score_context_for_task(task, payloads, paths)
    completion = _completion_quality(
        task,
        score,
        payloads,
        paths,
        sufficient_threshold=0.9,
    )

    assert score["path_coverage"] == 1.0
    assert score["evidence_coverage"] == 1.0
    assert score["sufficiency"] == 1.0
    assert completion["work_completed"] is True


def test_architecture_contract_uses_concepts_not_fixture_numbers():
    terms = COMPLETION_CONTRACTS["abstract-result-wrap"]["quality_terms"]

    assert "score" in terms
    assert not any(term.replace(".", "", 1).isdigit() for term in terms)
