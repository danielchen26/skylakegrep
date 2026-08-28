# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

from skylakegrep.src.cli import (
    _filter_results_to_explicit_scope,
    _lexical_evidence_satisfies_depth,
    _suppress_nonterminal_out_of_scope_for_scope,
)
from skylakegrep.src.llm_router import RouterDecision
from skylakegrep.src.query_scope import resolve_scope_facet


class _Decision:
    intent = "semantic"


def test_resolves_named_folder_scope_without_domain_terms(tmp_path: Path):
    scoped = tmp_path / "CASE42"
    scoped.mkdir()
    (tmp_path / "other").mkdir()

    facet = resolve_scope_facet(
        "show me where the project brief I recently created in CASE42 folder is",
        tmp_path,
    )

    assert facet is not None
    assert facet.root == scoped.resolve()
    assert facet.label == "CASE42"
    assert facet.confidence >= 0.9


def test_scope_prefers_non_copy_directory(tmp_path: Path):
    canonical = tmp_path / "Workspace" / "CASE42"
    duplicate = tmp_path / "Workspace 2" / "CASE42"
    canonical.mkdir(parents=True)
    duplicate.mkdir(parents=True)

    facet = resolve_scope_facet("find the report in CASE42 folder", tmp_path)

    assert facet is not None
    assert facet.root == canonical.resolve()


def test_resolves_cjk_folder_label_with_english_scope_marker(tmp_path: Path):
    scoped = tmp_path / "合同档案"
    scoped.mkdir()

    facet = resolve_scope_facet("where is 合同 file in 合同档案 folder", tmp_path)

    assert facet is not None
    assert facet.root == scoped.resolve()
    assert facet.label == "合同档案"


def test_resolves_cjk_scope_marker_and_suffix(tmp_path: Path):
    scoped = tmp_path / "合同档案"
    scoped.mkdir()

    facet = resolve_scope_facet("合同文件在合同档案文件夹", tmp_path)

    assert facet is not None
    assert facet.root == scoped.resolve()


def test_resolves_cjk_scope_clause_before_continuing_question(tmp_path: Path):
    scoped = tmp_path / "合同档案"
    scoped.mkdir()

    facet = resolve_scope_facet(
        "合同摘要在合同档案文件夹说明了什么 renewal process",
        tmp_path,
    )

    assert facet is not None
    assert facet.root == scoped.resolve()


def test_scope_filter_drops_absolute_paths_outside_explicit_root(tmp_path: Path):
    scoped = tmp_path / "CASE42"
    outside = tmp_path / "OTHER42"
    scoped.mkdir()
    outside.mkdir()
    inside_file = scoped / "project_report.md"
    outside_file = outside / "project_report.md"
    inside_file.write_text("inside")
    outside_file.write_text("outside")

    filtered = _filter_results_to_explicit_scope(
        [
            {"path": str(inside_file), "score": 1.0},
            {"path": str(outside_file), "score": 0.9},
            {"path": "relative_hit.md", "score": 0.8},
        ],
        scoped,
    )

    assert [Path(r["path"]).name for r in filtered] == [
        "project_report.md",
        "relative_hit.md",
    ]


def test_explicit_scope_suppresses_nonterminal_metadata_hint():
    decision = RouterDecision(
        intent="semantic",
        confidence=0.9,
        out_of_scope="recency",
        reason="model guessed metadata from temporal wording",
    )

    _suppress_nonterminal_out_of_scope_for_scope(
        "explain renewal logic in CASE42 folder",
        decision,
        explicit_scope=True,
    )

    assert decision.out_of_scope == "none"


def test_explicit_scope_keeps_terminal_metadata_hint():
    decision = RouterDecision(
        intent="mixed",
        confidence=0.9,
        out_of_scope="recency",
        reason="terminal metadata query",
    )

    _suppress_nonterminal_out_of_scope_for_scope(
        "show recently created files in CASE42 folder",
        decision,
        explicit_scope=True,
    )

    assert decision.out_of_scope == "recency"


def test_lexical_evidence_can_satisfy_default_preview_depth():
    assert _lexical_evidence_satisfies_depth(
        "what does retry policy say about backoff",
        [
            {
                "path": "src/retry_policy.py",
                "snippet": "Retry policy uses exponential backoff with jitter.",
                "chunk": "Retry policy uses exponential backoff with jitter.",
                "lexical_score": 0.6,
            }
        ],
        _Decision(),
        detail="auto",
        answer=False,
        agentic=False,
    )


def test_lexical_evidence_uses_scope_stripping_and_surface_variants():
    assert _lexical_evidence_satisfies_depth(
        "how are request budgets enforced in CASE42 folder",
        [
            {
                "path": "docs/rate_limits.md",
                "snippet": "The service budget is 120 requests per minute.",
                "chunk": "The service budget is 120 requests per minute.",
                "lexical_score": 0.2,
            }
        ],
        _Decision(),
        detail="auto",
        answer=False,
        agentic=False,
    )


def test_lexical_evidence_does_not_short_circuit_agentic_depth():
    assert not _lexical_evidence_satisfies_depth(
        "what does retry policy say about backoff",
        [
            {
                "path": "src/retry_policy.py",
                "snippet": "Retry policy uses exponential backoff with jitter.",
                "chunk": "Retry policy uses exponential backoff with jitter.",
                "lexical_score": 0.6,
            }
        ],
        _Decision(),
        detail="auto",
        answer=True,
        agentic=False,
    )
