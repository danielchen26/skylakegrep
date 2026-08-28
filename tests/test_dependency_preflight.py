# SPDX-License-Identifier: Apache-2.0
"""Dependency preflight: classifier, redaction, and registry integrity.

No network. The probe's transport is exercised elsewhere; what is pinned
here is the reasoning applied to a response, because that reasoning is what
turns an observation into a published claim — and the first version of it
was wrong in both directions.
"""

from __future__ import annotations

import json

import pytest

from benchmarks import dependency_preflight as dp

HF = "https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/onnx/model.onnx"
BLOCK_PAGE = (
    "https://sase.example.com/block_ai.html?url=https%3a%2f%2fhuggingface%2eco"
    "&reason=Not+allowed+to+browse+Blocked+AI+Domains+category"
    "&reasoncode=CATEGORY_DENIED&user=someone@example.com&zsq=MTRn5qTKfsTW5ssPvjS5"
)


# --- reachability, not endpoint semantics -----------------------------


@pytest.mark.parametrize("status", [200, 206, 301, 404, 405, 500])
def test_any_answer_from_the_origin_host_counts_as_reachable(status):
    """Regression: an earlier classifier scored 404 from registry.ollama.ai and
    405 from api.mixedbread.com as failures, and published a receipt claiming
    every tool was unavailable. The question is whether the host answers."""

    result = dp.classify_response(HF, status, HF, "")
    assert result.measured_status == "reachable"
    assert result.http_status == status


def test_redirect_to_another_host_is_a_policy_block():
    result = dp.classify_response(HF, 307, BLOCK_PAGE, "")
    assert result.measured_status == "blocked_by_policy"
    assert "sase.example.com" in result.detail
    assert "huggingface.co" in result.detail


def test_filter_marker_in_the_body_is_a_policy_block_even_with_status_200():
    """Some gateways serve the block page in place, with a 200."""

    result = dp.classify_response(
        HF, 200, HF, "<html>Access Denied - URL Filtering policy</html>"
    )
    assert result.measured_status == "blocked_by_policy"


def test_same_host_redirect_is_not_a_block():
    result = dp.classify_response(
        HF, 302, "https://huggingface.co/somewhere/else", ""
    )
    assert result.measured_status == "reachable"


# --- redaction --------------------------------------------------------


def test_block_page_evidence_is_stripped_of_identity():
    """Receipts are public artifacts; the gateway names the logged-in user."""

    cleaned = dp.redact(BLOCK_PAGE)
    assert "someone@example.com" not in cleaned
    assert "MTRn5qTKfsTW5ssPvjS5" not in cleaned
    # The part that carries the finding survives.
    assert "CATEGORY_DENIED" in cleaned
    assert "Blocked+AI+Domains" in cleaned


@pytest.mark.parametrize(
    "raw",
    [
        "user=alice@corp.example",
        "USERNAME=bob",
        "login=carol&next=/x",
        "contact dave@example.org for access",
    ],
)
def test_redaction_covers_identity_shapes(raw):
    cleaned = dp.redact(raw)
    assert not any(
        token in cleaned
        for token in ("alice@corp.example", "bob", "carol", "dave@example.org")
    )


def test_classified_block_never_carries_identity_through():
    result = dp.classify_response(HF, 307, BLOCK_PAGE, "")
    assert "someone@example.com" not in json.dumps(result.__dict__)


# --- registry integrity ----------------------------------------------


def test_every_declared_claim_has_a_source():
    """A declared egress class without a citation is marketing."""

    for dep in dp.REGISTRY:
        assert dep.declared_source.strip(), dep.tool
        assert dep.declared_egress in dp.EGRESS_CLASSES


def test_registry_rejects_an_unknown_egress_class():
    with pytest.raises(ValueError, match="declared_egress"):
        dp.ToolDependency(
            tool="x", binary="x", install="x",
            declared_model_urls=(), declared_egress="maybe",
            declared_source="s",
        )


def test_registry_rejects_an_unsourced_claim():
    with pytest.raises(ValueError, match="source"):
        dp.ToolDependency(
            tool="x", binary="x", install="x",
            declared_model_urls=(), declared_egress="none",
            declared_source="   ",
        )


def test_a_tool_with_no_model_dependency_is_not_applicable_not_available():
    """ripgrep has no semantic mode to be available or unavailable."""

    report = dp.evaluate(
        deps=tuple(d for d in dp.REGISTRY if d.tool == "ripgrep"), timeout=1.0
    )
    assert report["tools"][0]["measured_semantic_mode"] == "not_applicable"


# --- receipt shape ----------------------------------------------------


def test_receipt_separates_measured_from_declared_and_names_the_network():
    report = dp.evaluate(
        deps=tuple(d for d in dp.REGISTRY if d.tool == "ripgrep"), timeout=1.0
    )
    row = report["tools"][0]
    assert {"declared_egress", "declared_source", "measured_binary_present"} <= set(row)
    # A blocked probe is a fact about a network, so the receipt must say which.
    assert "note" in report["network"]
    assert "integrity" in report["definition"]


def test_evaluate_marks_semantic_unavailable_when_a_model_url_is_blocked(monkeypatch):
    monkeypatch.setattr(
        dp,
        "probe",
        lambda url, timeout=0: dp.ProbeResult(
            url=url, measured_status="blocked_by_policy", http_status=307
        ),
    )
    report = dp.evaluate(
        deps=tuple(d for d in dp.REGISTRY if d.tool == "ck"), timeout=1.0
    )
    assert report["tools"][0]["measured_semantic_mode"] == "unavailable"


def test_evaluate_marks_semantic_available_when_reachable(monkeypatch):
    monkeypatch.setattr(
        dp,
        "probe",
        lambda url, timeout=0: dp.ProbeResult(
            url=url, measured_status="reachable", http_status=200
        ),
    )
    report = dp.evaluate(
        deps=tuple(d for d in dp.REGISTRY if d.tool == "skylakegrep"), timeout=1.0
    )
    assert report["tools"][0]["measured_semantic_mode"] == "available"
