"""Tests for closed-loop benchmark helpers."""

from __future__ import annotations

from benchmarks.closed_loop_agent_benchmark import _path_probe_rank, _probe_terms


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
