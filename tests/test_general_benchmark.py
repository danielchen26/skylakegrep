"""Contracts for the public General Benchmark v2."""

from __future__ import annotations

import os
import subprocess

import pytest

from benchmarks.general_capacity_preflight import capacity_report
from benchmarks.general_stats import paired_efficiency
from benchmarks.merge_general_reports import merge_reports
from benchmarks.public_fixtures import RepoSpec, load_registry, prepare_repo, validate_repo_fixture
from benchmarks.token_savings import approximate_tokens, tokenizer_metadata
from benchmarks.universal_closed_loop_benchmark import (
    PROJECT_ROOT as UNIVERSAL_PROJECT_ROOT,
    _attach_source_gate,
    _run as _universal_run,
)


def _row(
    repo: str,
    task: str,
    policy: str,
    *,
    tokens: int,
    calls: int,
    elapsed: float,
    quality: float = 1.0,
    completed: bool = True,
):
    return {
        "repo": repo,
        "task_id": task,
        "effort": "medium",
        "trial": 1,
        "policy": policy,
        "context_tokens": tokens,
        "tool_calls": calls,
        "elapsed_seconds": elapsed,
        "tool_elapsed_seconds": elapsed,
        "path_coverage": 1.0,
        "path_precision": 1.0,
        "evidence_coverage": 1.0,
        "sufficiency": 1.0,
        "task_completion_quality": quality,
        "work_completed": completed,
    }


def test_universal_run_exposes_benchmark_checkout_to_external_cwd(tmp_path, monkeypatch):
    captured = {}

    def fake_subprocess_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    monkeypatch.setenv("PYTHONPATH", "/existing/path")

    _universal_run(["python", "-V"], tmp_path, timeout=1)

    pythonpath = captured["env"]["PYTHONPATH"].split(os.pathsep)
    assert pythonpath[0] == str(UNIVERSAL_PROJECT_ROOT)
    assert pythonpath[1] == "/existing/path"


def _cobra_capacity_receipt(*, elapsed: float = 10.0, chunks: int = 10):
    return {
        "schema_version": 3,
        "environment": {
            "benchmark_source_commit": "1" * 40,
            "benchmark_source_tracked_clean": "true",
        },
        "generalization": {"source_gate": {"passed": True}},
        "sections": [
            {
                "repo": "cobra",
                "index": {
                    "refreshed": True,
                    "reset": True,
                    "integrity": "ok",
                    "chunks": chunks,
                    "elapsed_seconds": elapsed,
                },
            }
        ],
    }


def test_public_registry_has_six_pinned_repos_and_sixty_evidence_tasks():
    registry = load_registry()
    assert set(registry) == {"cobra", "django", "react", "spring-framework", "tokio", "vite"}
    assert sum(len(spec.tasks) for spec in registry.values()) == 60
    for spec in registry.values():
        assert len(spec.commit) == 40
        assert spec.url.startswith("https://github.com/")
        for task in spec.tasks:
            assert task["deliverable"] == "source_evidence"
            assert len(task["evidence_terms"]) >= 2
            assert len(task["quality_terms"]) >= 2


def test_capacity_preflight_uses_conservative_chunk_or_character_ratio():
    workloads = {
        repo: {"files": 1, "chunks": 10, "model_chars": 1_000}
        for repo in load_registry()
    }
    workloads["django"] = {"files": 20, "chunks": 20, "model_chars": 5_000}
    workloads["react"] = {"files": 30, "chunks": 40, "model_chars": 2_000}
    report = capacity_report(
        workloads,
        _cobra_capacity_receipt(),
        max_index_seconds=45.0,
    )
    assert report["repositories"]["django"]["capacity_load_ratio"] == 5.0
    assert report["repositories"]["django"]["projected_index_seconds"] == 50.0
    assert report["repositories"]["react"]["capacity_load_ratio"] == 4.0
    assert report["capacity_gate"] == {
        "passed": False,
        "limiting_repo": "django",
        "largest_projected_index_seconds": 50.0,
    }


def test_capacity_preflight_rejects_stale_or_mismatched_cobra_receipt():
    workloads = {
        repo: {"files": 1, "chunks": 10, "model_chars": 1_000}
        for repo in load_registry()
    }
    dirty = _cobra_capacity_receipt()
    dirty["environment"]["benchmark_source_tracked_clean"] = "false"
    with pytest.raises(ValueError, match="clean, source-gated"):
        capacity_report(workloads, dirty, max_index_seconds=100.0)

    mismatched = _cobra_capacity_receipt(chunks=9)
    with pytest.raises(ValueError, match="chunk count"):
        capacity_report(workloads, mismatched, max_index_seconds=100.0)


def test_public_fixture_validation_requires_origin_pin_and_clean_tracked_tree(tmp_path):
    repo = tmp_path / "public"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/public.git"],
        cwd=repo,
        check=True,
    )
    source = repo / "source.py"
    source.write_text("class PublicAnchor:\n    def source_fact(self): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Benchmark Test",
            "-c",
            "user.email=benchmark",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    spec = RepoSpec(
        key="public",
        label="Public",
        url="https://github.com/example/public.git",
        subdir="public",
        commit=commit,
        fixture=tmp_path / "fixture.json",
        tasks=(
            {
                "id": "public-task",
                "expected": "source.py",
                "expected_alternatives": [],
                "evidence_terms": ["PublicAnchor", "source_fact"],
                "quality_terms": ["PublicAnchor", "source_fact"],
            },
        ),
    )
    assert validate_repo_fixture(repo, spec) == []
    source.write_text("tracked modification\n", encoding="utf-8")
    failures = validate_repo_fixture(repo, spec)
    assert "tracked worktree differs from the pinned commit" in failures


def test_prepare_repo_materializes_a_fresh_no_checkout_clone(tmp_path):
    source_repo = tmp_path / "source"
    source_repo.mkdir()
    subprocess.run(["git", "init"], cwd=source_repo, check=True, capture_output=True)
    source = source_repo / "source.py"
    source.write_text("class PublicAnchor:\n    def source_fact(self): pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "source.py"], cwd=source_repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Benchmark Test",
            "-c",
            "user.email=benchmark",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=source_repo,
        check=True,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    spec = RepoSpec(
        key="public",
        label="Public",
        url=str(source_repo),
        subdir="public",
        commit=commit,
        fixture=tmp_path / "fixture.json",
        tasks=(
            {
                "id": "public-task",
                "expected": "source.py",
                "expected_alternatives": [],
                "evidence_terms": ["PublicAnchor", "source_fact"],
                "quality_terms": ["PublicAnchor", "source_fact"],
            },
        ),
    )
    prepared = prepare_repo(spec, tmp_path / "clones")
    assert (prepared / "source.py").is_file()
    assert validate_repo_fixture(prepared, spec) == []


def test_paired_efficiency_reports_distribution_only_after_quality_gate():
    rows = []
    for repo in ("one", "two"):
        rows.extend(
            [
                _row(repo, "task", "skygrep-first", tokens=100, calls=1, elapsed=2.0),
                _row(repo, "task", "rg-only", tokens=400, calls=8, elapsed=4.0),
            ]
        )
    report = paired_efficiency(
        rows,
        bootstrap_samples=200,
        bootstrap_seed=7,
        min_pairs=2,
        min_repos=2,
    )
    assert report["claim_status"] == "reportable"
    assert report["quality_gate"]["passed"] is True
    assert report["quality_eligible_pairs"] == 2
    assert report["paired_distributions"]["context_token_reduction_x"]["median"] == 4.0
    assert report["paired_distributions"]["tool_call_reduction_x"]["median"] == 8.0
    assert report["paired_distributions"]["measured_elapsed_ratio_x"]["median"] == 2.0
    assert report["paired_distributions"]["measured_elapsed_delta_seconds"]["median"] == -2.0
    assert report["by_repo"]["one"]["measured_elapsed_ratio_x"]["median"] == 2.0
    assert report["context_token_median_95pct_ci"]["low"] == 4.0
    assert report["context_token_median_95pct_ci"]["high"] == 4.0


def test_paired_efficiency_refuses_multiplier_when_treatment_quality_regresses():
    rows = [
        _row("one", "task", "skygrep-first", tokens=10, calls=1, elapsed=1.0, quality=0.5),
        _row("one", "task", "rg-only", tokens=1000, calls=20, elapsed=10.0, quality=1.0),
    ]
    report = paired_efficiency(rows, bootstrap_samples=20)
    assert report["claim_status"] == "insufficient"
    assert report["quality_gate"]["passed"] is False
    assert report["quality_eligible_pairs"] == 0
    assert report["paired_distributions"]["context_token_reduction_x"]["median"] is None


def test_paired_efficiency_refuses_general_claim_for_tiny_sample():
    rows = [
        _row("one", "task", "skygrep-first", tokens=10, calls=1, elapsed=1.0),
        _row("one", "task", "rg-only", tokens=100, calls=10, elapsed=2.0),
    ]
    report = paired_efficiency(rows, bootstrap_samples=20)
    assert report["quality_gate"]["passed"] is True
    assert report["sample_gate"]["passed"] is False
    assert report["claim_status"] == "insufficient"


def test_source_gate_downgrades_a_dirty_or_unknown_benchmark_source():
    report = _attach_source_gate(
        {"claim_status": "reportable"},
        {
            "benchmark_source_commit": "unknown",
            "benchmark_source_tracked_clean": "false",
        },
    )
    assert report["claim_status"] == "insufficient"
    assert report["source_gate"]["passed"] is False


def test_repeated_trials_do_not_inflate_unique_task_sample_gate():
    rows = []
    for trial in range(1, 31):
        sky = _row("one", "same-task", "skygrep-first", tokens=10, calls=1, elapsed=1.0)
        rg = _row("one", "same-task", "rg-only", tokens=100, calls=10, elapsed=2.0)
        sky["trial"] = trial
        rg["trial"] = trial
        rows.extend([sky, rg])
    report = paired_efficiency(rows, bootstrap_samples=20, min_pairs=30, min_repos=1)
    assert report["quality_eligible_pairs"] == 30
    assert report["quality_eligible_tasks"] == 1
    assert report["sample_gate"]["passed"] is False
    assert report["claim_status"] == "insufficient"


def test_full_matrix_efforts_do_not_inflate_unique_task_sample_gate():
    rows = []
    for effort in ("low", "medium", "high"):
        sky = _row("one", "same-task", "skygrep-first", tokens=10, calls=1, elapsed=1.0)
        rg = _row("one", "same-task", "rg-only", tokens=100, calls=10, elapsed=2.0)
        sky["effort"] = effort
        rg["effort"] = effort
        rows.extend([sky, rg])
    report = paired_efficiency(rows, bootstrap_samples=20, min_pairs=3, min_repos=1)
    assert report["quality_eligible_pairs"] == 3
    assert report["quality_eligible_tasks"] == 1
    assert report["sample_gate"]["passed"] is False
    assert report["claim_status"] == "insufficient"


def test_parallel_repo_receipts_merge_before_general_gate():
    reports = []
    registry = load_registry()
    repo_names = tuple(registry)
    for repo, spec in registry.items():
        rows = []
        for task_spec in spec.tasks:
            task = str(task_spec["id"])
            rows.extend(
                [
                    _row(repo, task, "skygrep-first", tokens=100, calls=1, elapsed=2.0),
                    _row(repo, task, "rg-only", tokens=400, calls=8, elapsed=1.0),
                ]
            )
        reports.append(
            {
                "schema_version": 3,
                "definition": {
                    "policies": ["skygrep-first", "rg-only"],
                    "mode": "adaptive-only",
                },
                "parameters": {
                    "repos": [repo],
                    "max_tasks_per_repo": None,
                    "tokenizer": {"actual": "tiktoken:cl100k_base"},
                    "trials": 1,
                    "tokens_per_second": 30_000.0,
                    "sufficient_threshold": 0.85,
                    "timeout_seconds": 45.0,
                    "allow_root_fallback": False,
                    "quality_floor": 0.85,
                    "noninferiority_margin_pct": 2.0,
                    "bootstrap_samples": 20,
                    "bootstrap_seed": 7,
                    "min_general_tasks": 30,
                    "min_general_repos": 3,
                    "benchmark_wall_seconds": 1.0,
                },
                "environment": {
                    "system": "test",
                    "benchmark_source_commit": "1" * 40,
                    "benchmark_source_tracked_clean": "true",
                },
                "sections": [{"repo": repo, "tasks": 10, "commit": spec.commit}],
                "rows": rows,
            }
        )
    merged = merge_reports(reports)
    assert merged["parameters"]["repos"] == sorted(repo_names)
    assert merged["generalization"]["claim_status"] == "reportable"
    assert merged["generalization"]["quality_eligible_tasks"] == 60
    assert merged["generalization"]["paired_distributions"]["context_token_reduction_x"]["median"] == 4.0
    assert merged["generalization"]["source_gate"]["passed"] is True
    assert merged["environment"]["benchmark_source_commit"] == "1" * 40
    assert merged["rows"] == []
    with pytest.raises(ValueError, match="receipt set is incomplete"):
        merge_reports(reports[:-1])

    mismatched = [dict(report) for report in reports]
    mismatched[-1] = {
        **mismatched[-1],
        "parameters": {**mismatched[-1]["parameters"], "trials": 2},
    }
    with pytest.raises(ValueError, match="same trials"):
        merge_reports(mismatched)

    wrong_pin = [dict(report) for report in reports]
    wrong_pin[0] = {
        **wrong_pin[0],
        "sections": [{**wrong_pin[0]["sections"][0], "commit": "0" * 40}],
    }
    with pytest.raises(ValueError, match="receipt commit"):
        merge_reports(wrong_pin)

    incomplete = [dict(report) for report in reports]
    incomplete[0] = {**incomplete[0], "rows": incomplete[0]["rows"][:-1]}
    with pytest.raises(ValueError, match="incomplete task observations"):
        merge_reports(incomplete)

    unexpected_policy = [dict(report) for report in reports]
    unexpected_rows = list(unexpected_policy[0]["rows"])
    unexpected_rows[0] = {**unexpected_rows[0], "policy": "other"}
    unexpected_policy[0] = {**unexpected_policy[0], "rows": unexpected_rows}
    with pytest.raises(ValueError, match="unexpected policy"):
        merge_reports(unexpected_policy)

    dirty_source = [dict(report) for report in reports]
    dirty_source[0] = {
        **dirty_source[0],
        "environment": {
            **dirty_source[0]["environment"],
            "benchmark_source_tracked_clean": "false",
        },
    }
    with pytest.raises(ValueError, match="clean tracked benchmark worktree"):
        merge_reports(dirty_source)

    mixed_source = [dict(report) for report in reports]
    mixed_source[0] = {
        **mixed_source[0],
        "environment": {
            **mixed_source[0]["environment"],
            "benchmark_source_commit": "2" * 40,
        },
    }
    with pytest.raises(ValueError, match="same benchmark source commit"):
        merge_reports(mixed_source)


def test_chars_tokenizer_is_explicit_fallback(monkeypatch):
    monkeypatch.setenv("SKYGREP_BENCH_TOKENIZER", "chars")
    assert approximate_tokens("abcdefgh", 4) == 2
    assert tokenizer_metadata() == {
        "requested": "chars",
        "actual": "chars-per-token",
        "exact_tokenizer": False,
        "encoding": None,
    }
