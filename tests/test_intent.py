"""Tests for the v0.14.0 intent classifier + multi-tier merger."""

from __future__ import annotations

import pytest

from local_mgrep.src.intent import classify_intent, merge_results


# ---- classify_intent ------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "where is eb1b file?",
        "find package.json",
        "show me the README",
        "locate config.toml",
        "open .env",
        "find auth_token.py",
    ],
)
def test_filename_intent_detected(query):
    assert classify_intent(query) == "filename"


@pytest.mark.parametrize(
    "query",
    [
        "how does the auth token get refreshed",
        "explain the cascade decision logic",
        "why does the indexer skip binary files",
        "what does spawn_background_index do",
        "describe the lexical pre-gate flow",
    ],
)
def test_semantic_intent_detected(query):
    assert classify_intent(query) == "semantic"


@pytest.mark.parametrize(
    "query",
    [
        "auth login",
        "config defaults",
        "rerank pool",
    ],
)
def test_lexical_intent_detected(query):
    assert classify_intent(query) == "lexical"


def test_long_query_is_semantic():
    """Six or more meaningful words → likely descriptive question."""
    q = "session token refresh middleware request handler dispatch"
    assert classify_intent(q) == "semantic"


def test_empty_query_is_mixed():
    assert classify_intent("") == "mixed"
    assert classify_intent("   ") == "mixed"


def test_extension_in_query_forces_filename():
    """Even without an intent verb, an explicit extension hints
    filename intent."""
    assert classify_intent("config.py") == "filename"
    assert classify_intent("README.md") == "filename"


# ---- merge_results --------------------------------------------------


def _r(path: str, score: float, fallback: str) -> dict:
    return {"path": path, "score": score, "fallback": fallback}


def test_merge_filename_intent_promotes_filename_results():
    """Under filename intent, filename hits should rank above
    semantic hits even if semantic scores are higher."""
    fn = [_r("eb1b.pdf", 1.0, "filename-lookup")]
    sem = [_r("other.py", 4.5, "cascade"), _r("more.py", 3.2, "cascade")]
    merged = merge_results(
        filename=fn, lexical=[], semantic=sem,
        intent="filename", top_k=5,
    )
    # filename result must be first
    assert merged[0]["path"] == "eb1b.pdf"
    assert len(merged) == 3


def test_merge_semantic_intent_promotes_cascade_results():
    """Under semantic intent, cascade results dominate."""
    fn = [_r("foo.py", 1.0, "filename-lookup")]
    sem = [_r("auth.py", 4.5, "cascade"), _r("login.py", 3.2, "cascade")]
    merged = merge_results(
        filename=fn, lexical=[], semantic=sem,
        intent="semantic", top_k=5,
    )
    assert merged[0]["path"] == "auth.py"
    assert merged[1]["path"] == "login.py"
    # filename result still surfaced last
    assert merged[-1]["path"] == "foo.py"


def test_merge_dedupes_by_path_keeping_higher_priority_tier():
    """Same path appearing in multiple tiers shouldn't double-count.
    Under filename intent, the filename tier's representation wins."""
    same = "config.py"
    fn = [_r(same, 1.0, "filename-lookup")]
    sem = [_r(same, 4.5, "cascade")]
    merged = merge_results(
        filename=fn, lexical=[], semantic=sem,
        intent="filename", top_k=5,
    )
    assert len(merged) == 1
    assert merged[0]["fallback"] == "filename-lookup"


def test_merge_dedupes_by_path_under_semantic_intent_keeps_cascade():
    same = "config.py"
    fn = [_r(same, 1.0, "filename-lookup")]
    sem = [_r(same, 4.5, "cascade")]
    merged = merge_results(
        filename=fn, lexical=[], semantic=sem,
        intent="semantic", top_k=5,
    )
    assert len(merged) == 1
    assert merged[0]["fallback"] == "cascade"


def test_merge_respects_top_k():
    fn = [_r(f"f{i}.pdf", 1.0, "filename-lookup") for i in range(10)]
    sem = [_r(f"s{i}.py", 4.0 - i * 0.1, "cascade") for i in range(10)]
    merged = merge_results(
        filename=fn, lexical=[], semantic=sem,
        intent="mixed", top_k=5,
    )
    assert len(merged) == 5


def test_merge_handles_empty_inputs():
    merged = merge_results(
        filename=None, lexical=None, semantic=[],
        intent="mixed", top_k=5,
    )
    assert merged == []


def test_merge_lexical_intent_promotes_rg_shortcut():
    rg = [_r("auth/login.py", 0.8, "rg-shortcut")]
    sem = [_r("other.py", 4.5, "cascade")]
    merged = merge_results(
        filename=[], lexical=rg, semantic=sem,
        intent="lexical", top_k=5,
    )
    assert merged[0]["path"] == "auth/login.py"
