# SPDX-License-Identifier: Apache-2.0
"""Tests for closed-loop benchmark helpers."""

from __future__ import annotations

import importlib.util
import json
import subprocess
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
_read_paths_step = _benchmark._read_paths_step
_read_symbol_paths_step = _benchmark._read_symbol_paths_step
_candidate_scope_source_paths = _benchmark._candidate_scope_source_paths
_filename_probe_step = _benchmark._filename_probe_step
_safe_rel = _benchmark._safe_rel
_skygrep_step = _benchmark._skygrep_step
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


def test_probe_terms_add_compound_and_inflection_variants():
    terms = _probe_terms("Where is Ctrl-C handling and form cleaning implemented?", max_terms=12)

    assert "ctrl" in terms
    assert "clean" in terms
    assert "implemented" not in terms


def test_path_probe_rank_prefers_source_over_docs_when_term_evidence_ties():
    terms = {"url", "view", "function"}

    source_rank = _path_probe_rank("django/urls/base.py", terms)
    docs_rank = _path_probe_rank("docs/topics/http/urls.txt", terms)

    assert source_rank < docs_rank


def test_safe_rel_normalizes_paths_through_a_symlinked_root(tmp_path):
    real_root = tmp_path / "real"
    real_root.mkdir()
    source = real_root / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("def main():\n    return 0\n", encoding="utf-8")
    alias_root = tmp_path / "alias"
    alias_root.symlink_to(real_root, target_is_directory=True)

    assert _safe_rel(alias_root, str(alias_root / "src" / "main.py")) == "src/main.py"


def test_skygrep_paths_feed_candidate_reads_and_relative_globs(tmp_path, monkeypatch):
    real_root = tmp_path / "real"
    real_root.mkdir()
    source = real_root / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("def main():\n    return 0\n", encoding="utf-8")
    alias_root = tmp_path / "alias"
    alias_root.symlink_to(real_root, target_is_directory=True)
    payload = json.dumps([{"path": str(alias_root / "src" / "main.py")}])

    monkeypatch.setattr(
        _benchmark,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=[], returncode=0, stdout=payload, stderr=""),
    )
    search = _skygrep_step(
        alias_root,
        "where is main",
        timeout=1,
        top=5,
        detail="standard",
        name="test:search",
    )
    read = _read_paths_step(
        alias_root,
        search.paths,
        max_files=1,
        max_chars=1_000,
        name="test:read",
    )

    assert search.paths == ["src/main.py"]
    assert _benchmark._candidate_globs(search.paths) == ["src/main.py"]
    assert read.paths == ["src/main.py"]
    assert "def main" in read.payload


def test_symbol_read_finds_declarations_beyond_the_file_head(tmp_path):
    source = tmp_path / "state_machine.py"
    source.write_text(
        ("# unrelated setup\n" * 2_000)
        + "def initialize_cache_state():\n    pass\n"
        + ("# more unrelated setup\n" * 2_000)
        + "def dispatch_state_update():\n    pass\n",
        encoding="utf-8",
    )

    result = _read_symbol_paths_step(
        tmp_path,
        ["state_machine.py"],
        "where is cache state initialized and updated?",
        max_files=1,
        max_chars_per_file=2_000,
        name="test:symbols",
    )

    assert result.paths == ["state_machine.py"]
    assert result.tool_calls == 1
    assert "initialize_cache_state" in result.payload
    assert "dispatch_state_update" in result.payload
    assert result.context_tokens < 500


def test_symbol_read_keeps_strict_declarations_without_query_term_overlap(tmp_path):
    source = tmp_path / "reconciler.js"
    source.write_text(
        "function compareKeyedChildren() {}\n"
        "function updateSlot() {}\n",
        encoding="utf-8",
    )

    result = _read_symbol_paths_step(
        tmp_path,
        ["reconciler.js"],
        "where are keyed children compared and reused?",
        max_files=1,
        max_chars_per_file=1_000,
        name="test:symbols",
    )

    assert "compareKeyedChildren" in result.payload
    assert "updateSlot" in result.payload


def test_candidate_scope_expands_to_immediate_source_siblings(tmp_path):
    template = tmp_path / "src" / "template"
    template.mkdir(parents=True)
    (template / "base.py").write_text("class Template: pass\n", encoding="utf-8")
    (template / "engine.py").write_text("class Engine: pass\n", encoding="utf-8")
    (template / "notes.md").write_text("not source\n", encoding="utf-8")

    siblings = _candidate_scope_source_paths(
        tmp_path,
        ["src/template/base.py"],
        "where is the template engine?",
        max_scopes=2,
        max_files=10,
    )

    assert "src/template/base.py" in siblings
    assert "src/template/engine.py" in siblings
    assert "src/template/notes.md" not in siblings


def test_filename_probe_handles_compound_and_inflected_path_terms(tmp_path):
    paths = [
        "spring/core/io/DefaultResourceLoader.java",
        "tokio/src/sync/notify.rs",
        "tokio/src/signal/ctrl_c.rs",
        "tokio/src/task/spawn.rs",
    ]
    for rel in paths:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// source\n", encoding="utf-8")

    cases = {
        "where is the default resource loader?": "DefaultResourceLoader.java",
        "where is the notification primitive?": "notify.rs",
        "where is the Ctrl-C signal handler?": "ctrl_c.rs",
        "where does task spawning happen?": "spawn.rs",
    }
    for query, expected_suffix in cases.items():
        result = _filename_probe_step(tmp_path, query, max_paths=10, name="test:filename")
        assert any(path.endswith(expected_suffix) for path in result.paths)


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



# --- rank-based axes -------------------------------------------------

_rank_of_first_hit = _benchmark._rank_of_first_hit
_rank_metrics = _benchmark._rank_metrics
_aggregate = _benchmark._aggregate


def test_rank_of_first_hit_is_ordinal_not_set_membership():
    groups = [["command.go"]]
    paths = ["args.go", "flags.go", "command.go", "zsh_completions.go"]

    assert _rank_of_first_hit(groups, paths) == 3
    # Reordering must change the answer; that is the whole point of the metric.
    assert _rank_of_first_hit(groups, ["command.go", *paths[:2]]) == 1


def test_rank_is_none_when_the_expected_path_never_appears():
    metrics = _rank_metrics([["command.go"]], ["args.go", "flags.go"])

    assert metrics["rank_first_hit"] is None
    assert metrics["reciprocal_rank"] == 0.0
    assert metrics["hit_at_1"] == 0
    assert metrics["hit_at_3"] == 0


def test_hit_at_3_is_inclusive_and_hit_at_1_is_not():
    at_three = _rank_metrics([["c.go"]], ["a.go", "b.go", "c.go"])
    at_four = _rank_metrics([["d.go"]], ["a.go", "b.go", "c.go", "d.go"])

    assert (at_three["hit_at_3"], at_three["hit_at_1"]) == (1, 0)
    assert (at_four["hit_at_3"], at_four["hit_at_1"]) == (0, 0)


def test_any_group_alternative_satisfies_the_rank():
    metrics = _rank_metrics([["command.go", "cobra.go"]], ["args.go", "cobra.go"])

    assert metrics["rank_first_hit"] == 2
    assert metrics["reciprocal_rank"] == 0.5


def test_rank_separates_tools_that_precision_at_k_cannot():
    """Two retrievers, same 8 paths, one relevant file, opposite ranking.

    precision@8 is identical and pinned at 12.5% for both — the ceiling
    relevant/k imposes. Only the rank axis shows that one of them makes an
    agent open seven wrong files first."""

    groups = [["target.go"]]
    noise = [f"noise{i}.go" for i in range(7)]
    good = _rank_metrics(groups, ["target.go", *noise])
    bad = _rank_metrics(groups, [*noise, "target.go"])

    assert _benchmark._path_group_precision(
        groups, ["target.go", *noise]
    ) == _benchmark._path_group_precision(groups, [*noise, "target.go"])
    assert good["reciprocal_rank"] == 1.0
    assert bad["reciprocal_rank"] == 0.125
    assert (good["hit_at_1"], bad["hit_at_1"]) == (1, 0)


def test_scorer_reports_rank_alongside_the_published_sufficiency_definition():
    """Rank metrics are additive: sufficiency must keep its old value so
    receipts recorded before this axis existed stay comparable."""

    task = {
        "id": "unit-rank",
        "deliverable": "source_evidence",
        "expected_path_groups": [["command.go"]],
        "evidence_terms": ["ExecuteC", "execute"],
    }
    score = _score_context_for_task(
        task, ["func (c *Command) ExecuteC() {} execute"], ["args.go", "command.go"]
    )

    assert score["rank_first_hit"] == 2
    assert score["hit_at_3"] == 1
    assert score["sufficiency"] == round(0.6 * 1.0 + 0.4 * 1.0, 3)


def test_legacy_flat_expected_paths_also_get_rank_metrics():
    task = {
        "id": "unit-flat",
        "deliverable": "source_evidence",
        "expected_paths": ["command.go"],
        "evidence_terms": ["ExecuteC", "execute"],
    }
    score = _score_context_for_task(task, ["ExecuteC execute"], ["command.go"])

    assert score["rank_first_hit"] == 1
    assert score["reciprocal_rank"] == 1.0


def test_aggregate_rolls_up_mrr_hits_and_never_found_count():
    rows = [
        {
            "context_tokens": 100, "elapsed_seconds": 1.0, "tool_elapsed_seconds": 1.0,
            "sufficiency": 1.0, "task_completion_quality": 1.0, "tool_calls": 1,
            "path_coverage": 1.0, "path_precision": 0.125, "evidence_coverage": 1.0,
            "rank_first_hit": rank, "reciprocal_rank": (1.0 / rank if rank else 0.0),
            "hit_at_1": 1 if rank == 1 else 0,
            "hit_at_3": 1 if rank and rank <= 3 else 0,
        }
        for rank in (1, 2, None, 8)
    ]
    agg = _aggregate(rows, tokens_per_second=30000.0, sufficient_threshold=0.85)

    assert agg["hit_at_1_pct"] == 25.0
    assert agg["hit_at_3_pct"] == 50.0
    assert agg["mrr"] == round((1.0 + 0.5 + 0.0 + 0.125) / 4, 3)
    assert agg["tasks_never_found"] == 1
    # Mean rank is over found tasks only; a miss is not rank zero.
    assert agg["mean_rank_when_found"] == round((1 + 2 + 8) / 3, 2)