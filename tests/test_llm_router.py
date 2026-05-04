"""Tests for the v0.15.0 LLM-driven query router.

The LLM call itself is mocked — real tests would require a running
Ollama instance which CI doesn't have. These tests cover:

  - JSON parsing tolerance (raw output may include surrounding prose)
  - Confidence-based safety: low-confidence skip_cascade is overridden
  - Fallback chain: LLM unavailable → rule-based → safe-default
  - Cache reads/writes via SQLite
  - `route_query` returns a valid RouterDecision in every failure mode
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import pytest

from local_mgrep.src import llm_router as router
from local_mgrep.src.llm_router import RouterDecision, route_query


# ---- _parse_llm_json ------------------------------------------------


def test_parse_llm_json_clean():
    raw = '{"intent": "filename", "primary_token": "x", "confidence": 0.9}'
    parsed = router._parse_llm_json(raw)
    assert parsed["intent"] == "filename"
    assert parsed["confidence"] == 0.9


def test_parse_llm_json_tolerates_surrounding_prose():
    raw = """Here's the answer:

```json
{"intent": "semantic", "primary_token": "", "confidence": 0.8}
```

Hope that helps!"""
    parsed = router._parse_llm_json(raw)
    assert parsed["intent"] == "semantic"


def test_parse_llm_json_returns_none_on_garbage():
    assert router._parse_llm_json("not json at all") is None
    assert router._parse_llm_json("") is None
    assert router._parse_llm_json("{ broken: json") is None


# ---- _llm_decision: confidence safety -------------------------------


def test_low_confidence_overrides_skip_cascade():
    """LLM says skip_cascade=True with low confidence → must NOT skip."""
    fake_response = {
        "response": json.dumps({
            "intent": "filename",
            "primary_token": "x",
            "skip_cascade": True,
            "confidence": 0.5,  # below threshold 0.7
            "reason": "unsure",
        })
    }

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return fake_response

    with patch("local_mgrep.src.llm_router.requests.post",
               return_value=_FakeResp()):
        decision = router._llm_decision("test query")

    assert decision is not None
    assert decision.intent == "filename"
    assert decision.skip_cascade is False, (
        "low confidence must override skip_cascade=True"
    )
    assert decision.confidence == 0.5
    assert decision.source == "llm"


def test_high_confidence_skip_cascade_honored():
    fake_response = {
        "response": json.dumps({
            "intent": "filename",
            "primary_token": "eb1b",
            "skip_cascade": True,
            "extract_content": True,
            "confidence": 0.95,
            "reason": "clear filename lookup",
        })
    }

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return fake_response

    with patch("local_mgrep.src.llm_router.requests.post",
               return_value=_FakeResp()):
        decision = router._llm_decision("where is eb1b file?")

    assert decision is not None
    assert decision.skip_cascade is True
    assert decision.extract_content is True
    assert decision.primary_token == "eb1b"


# ---- _llm_decision: failure modes -----------------------------------


def test_llm_decision_returns_none_on_http_failure():
    import requests as r
    with patch(
        "local_mgrep.src.llm_router.requests.post",
        side_effect=r.ConnectionError("connection refused"),
    ):
        assert router._llm_decision("test") is None


def test_llm_decision_returns_none_on_malformed_json():
    fake_response = {"response": "not valid JSON at all"}

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return fake_response

    with patch("local_mgrep.src.llm_router.requests.post",
               return_value=_FakeResp()):
        assert router._llm_decision("test") is None


def test_llm_decision_clamps_invalid_intent_to_mixed():
    fake_response = {
        "response": json.dumps({
            "intent": "totally-bogus-intent",
            "confidence": 0.9,
        })
    }

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return fake_response

    with patch("local_mgrep.src.llm_router.requests.post",
               return_value=_FakeResp()):
        decision = router._llm_decision("test")
    assert decision.intent == "mixed"


# ---- _rule_based_decision (fallback-1) ------------------------------


def test_rule_based_filename_intent():
    d = router._rule_based_decision("where is eb1b file?")
    assert d.intent == "filename"
    assert d.source == "fallback-rules"


def test_rule_based_semantic_intent():
    d = router._rule_based_decision("how does the cascade decide")
    assert d.intent == "semantic"
    assert d.source == "fallback-rules"


def test_rule_based_empty_query_safe_default():
    d = router._rule_based_decision("")
    assert d.intent == "mixed"
    assert d.source == "fallback-mixed"


# ---- route_query end-to-end ----------------------------------------


def test_route_query_falls_back_when_llm_unavailable():
    """LLM HTTP raises → rule-based router takes over."""
    import requests as r
    with patch(
        "local_mgrep.src.llm_router.requests.post",
        side_effect=r.ConnectionError("ollama down"),
    ):
        d = route_query("where is foo file?")
    assert d.intent == "filename"
    assert d.source == "fallback-rules"


def test_route_query_use_llm_false_skips_llm_entirely():
    """`--no-llm-router` flag must never call requests.post."""
    with patch(
        "local_mgrep.src.llm_router.requests.post",
        side_effect=AssertionError("should not be called"),
    ):
        d = route_query("how does X work", use_llm=False)
    assert d.source == "fallback-rules"


def test_route_query_caches_in_sqlite(tmp_path):
    """Same query routed twice → second hit comes from cache, no LLM call."""
    db = tmp_path / "cache.sqlite"
    conn = sqlite3.connect(str(db))

    fake_response = {
        "response": json.dumps({
            "intent": "filename",
            "primary_token": "eb1b",
            "confidence": 0.9,
            "reason": "test",
        })
    }

    class _FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return fake_response

    with patch(
        "local_mgrep.src.llm_router.requests.post",
        return_value=_FakeResp(),
    ) as mocked:
        d1 = route_query("where is eb1b file?", conn=conn)
        d2 = route_query("where is eb1b file?", conn=conn)

    assert d1.intent == "filename"
    assert d2.intent == "filename"
    # First call hits LLM, second hits cache
    assert mocked.call_count == 1


def test_route_query_empty_query_safe_default():
    d = route_query("   ")
    assert d.intent == "mixed"
    assert d.source == "fallback-mixed"


def test_router_decision_dataclass_default_is_safe():
    """An empty RouterDecision should be safe (no skips)."""
    d = RouterDecision(intent="mixed")
    assert d.skip_cascade is False
    assert d.skip_filename is False
    assert d.skip_lexical is False
