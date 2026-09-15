# SPDX-License-Identifier: Apache-2.0
"""Egress audit and enforced offline mode.

These tests exist because the claim they defend is a procurement claim, not a
performance one: "answering a query needs no public network". A claim like that
is worth exactly as much as the check behind it, so the check is pinned here —
including the two ways an earlier version of it was wrong in the direction that
flattered the tool.
"""

from __future__ import annotations

import json

import pytest

from skylakegrep.src import egress


def _config(**overrides):
    base = {
        "ollama_url": "http://localhost:11434",
        "embed_model": "bge-m3",
        "llm_model": "qwen2.5:3b",
        "rerank_model": "mixedbread-ai/mxbai-rerank-large-v2",
    }
    base.update(overrides)
    return base


@pytest.fixture
def served(monkeypatch):
    """Control what the local Ollama reports, without touching the network."""

    tags: set[str] = set()

    def fake(url, timeout=3.0):
        return set(tags)

    monkeypatch.setattr(egress, "_ollama_models", fake)
    return tags


# --- tag matching: the false-positive that shipped --------------------


def test_a_different_tag_of_the_same_family_is_not_present(served):
    """Regression. `qwen2.5:3b` was reported present on a machine that only had
    `qwen2.5:7b`, because the check also matched the family before the colon.
    Tags are different weights, not aliases, and the error direction was a
    clean bill of health the machine had not earned."""

    served.add("qwen2.5:7b")
    deps = {d.name: d for d in egress.audit(_config())}

    assert deps["router llm"].satisfied_locally is False
    assert "ollama pull qwen2.5:3b" in deps["router llm"].remedy


def test_exact_tag_counts_as_present(served):
    served.update({"qwen2.5:3b", "bge-m3:latest"})
    deps = {d.name: d for d in egress.audit(_config())}

    assert deps["router llm"].satisfied_locally is True


@pytest.mark.parametrize(
    "configured,available,expected",
    [
        ("bge-m3", "bge-m3:latest", True),      # Ollama resolves bare -> :latest
        ("bge-m3:latest", "bge-m3", True),      # and the reverse
        ("bge-m3", "bge-m3:v2", False),         # but never across other tags
        ("bge-m3:v2", "bge-m3:latest", False),
    ],
)
def test_only_the_latest_equivalence_is_honoured(configured, available, expected):
    assert egress._tag_present(configured, {available}) is expected


def test_a_stopped_ollama_is_unknown_not_empty(monkeypatch):
    """A server that cannot be asked must not make every model look missing in
    a way that reads as a configuration error."""

    monkeypatch.setattr(egress, "_ollama_models", lambda url, timeout=3.0: None)
    deps = {d.name: d for d in egress.audit(_config())}

    assert deps["embedder"].satisfied_locally is False
    # Still loopback: an unreachable local server is not public egress.
    assert deps["embedder"].egress == egress.EGRESS_LOOPBACK


# --- egress classification -------------------------------------------


def test_loopback_and_internal_host_are_distinguished(served):
    served.add("bge-m3:latest")
    local = {d.name: d for d in egress.audit(_config())}
    remote = {
        d.name: d
        for d in egress.audit(_config(ollama_url="http://ollama.corp.internal:11434"))
    }

    assert local["embedder"].egress == egress.EGRESS_LOOPBACK
    assert remote["embedder"].egress == egress.EGRESS_INTERNAL
    # Both are "no public egress", but they are not the same review question.
    assert egress.EGRESS_PUBLIC not in {
        local["embedder"].egress,
        remote["embedder"].egress,
    }


def test_a_hub_repo_id_is_public_even_when_cached(monkeypatch, served):
    """Cached weights need no fetch today; the configured source is still
    public, and clearing the cache would dial out. The posture reports the
    configuration, `satisfied_locally` carries the nuance."""

    monkeypatch.setattr(egress, "hf_model_is_cached", lambda repo, cache_root=None: True)
    rerank = {d.name: d for d in egress.audit(_config())}["reranker"]

    assert rerank.egress == egress.EGRESS_PUBLIC
    assert rerank.satisfied_locally is True
    assert rerank.blocks_offline is False


def test_a_filesystem_reranker_has_no_egress_at_all(tmp_path, served):
    model_dir = tmp_path / "mxbai-local"
    model_dir.mkdir()
    rerank = {d.name: d for d in egress.audit(_config(rerank_model=str(model_dir)))}[
        "reranker"
    ]

    assert rerank.egress == egress.EGRESS_NONE
    assert rerank.satisfied_locally is True
    assert rerank.source.startswith("file:")


def test_a_repo_id_is_not_mistaken_for_a_path(served):
    """`org/name` contains a path separator; only existence on disk settles it."""

    assert egress._looks_like_path("mixedbread-ai/mxbai-rerank-large-v2") is False


def test_worst_egress_reports_the_most_exposed_dependency(served):
    served.update({"bge-m3:latest", "qwen2.5:3b"})

    assert egress.worst_egress(egress.audit(_config())) == egress.EGRESS_PUBLIC
    assert (
        egress.worst_egress(egress.audit(_config(rerank_model="/tmp")))
        == egress.EGRESS_LOOPBACK
    )


# --- hub cache detection ---------------------------------------------


def test_a_partial_hub_download_is_not_cached(tmp_path):
    """A refs-only or empty snapshot directory is exactly the state that would
    trigger a fetch, so it must not count as satisfied."""

    root = tmp_path / "hub"
    entry = root / "models--org--name"
    (entry / "snapshots").mkdir(parents=True)

    assert egress.hf_model_is_cached("org/name", root) is False

    (entry / "snapshots" / "abc123").mkdir()
    assert egress.hf_model_is_cached("org/name", root) is False

    (entry / "snapshots" / "abc123" / "config.json").write_text("{}")
    assert egress.hf_model_is_cached("org/name", root) is True


def test_hub_cache_root_honours_the_documented_variables(monkeypatch, tmp_path):
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    assert egress.hf_cache_root() == tmp_path / "hf" / "hub"

    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "explicit"))
    assert egress.hf_cache_root() == tmp_path / "explicit"


# --- enforcement ------------------------------------------------------


def test_offline_pins_the_hub_before_anything_imports_it():
    """sentence_transformers and huggingface_hub read these at import time, so
    pinning them afterwards is too late to matter."""

    env: dict[str, str] = {}
    egress.pin_hf_offline(env)

    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"


def test_pinning_never_overrides_a_deliberate_setting():
    env = {"HF_HUB_OFFLINE": "0"}
    egress.pin_hf_offline(env)

    assert env["HF_HUB_OFFLINE"] == "0"


def test_enforce_refuses_a_configuration_that_needs_a_public_fetch(monkeypatch, served):
    served.update({"bge-m3:latest", "qwen2.5:3b"})
    monkeypatch.setattr(egress, "hf_model_is_cached", lambda repo, cache_root=None: False)

    with pytest.raises(egress.OfflineViolation) as exc:
        egress.enforce(_config())

    message = str(exc.value)
    assert "mxbai-rerank-large-v2" in message
    # The error has to carry the fix; a filter timeout is what it replaces.
    assert "SKYGREP_RERANK_MODEL=" in message
    assert "would need a public fetch" in message


def test_enforce_passes_once_every_dependency_is_local(tmp_path, monkeypatch, served):
    served.update({"bge-m3:latest", "qwen2.5:3b"})
    model_dir = tmp_path / "local-reranker"
    model_dir.mkdir()

    deps = egress.enforce(_config(rerank_model=str(model_dir)))

    assert egress.worst_egress(deps) == egress.EGRESS_LOOPBACK
    assert all(not d.blocks_offline for d in deps)


def test_an_optional_dependency_still_blocks_offline_mode(monkeypatch, served):
    """Someone who asked for offline mode wants to be told reranking will not
    work here, not to find out mid-query."""

    served.update({"bge-m3:latest", "qwen2.5:3b"})
    monkeypatch.setattr(egress, "hf_model_is_cached", lambda repo, cache_root=None: False)
    rerank = {d.name: d for d in egress.audit(_config())}["reranker"]

    assert rerank.optional is True
    assert rerank.blocks_offline is True


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), ("true", True), ("yes", True), ("", False), ("0", False), ("off", False)],
)
def test_offline_requested_parses_the_usual_spellings(monkeypatch, value, expected):
    monkeypatch.setenv(egress.OFFLINE_ENV, value)
    assert egress.offline_requested() is expected


# --- rendering --------------------------------------------------------


def test_render_marks_a_blocking_dependency_and_shows_its_fix(monkeypatch, served):
    served.add("bge-m3:latest")
    monkeypatch.setattr(egress, "hf_model_is_cached", lambda repo, cache_root=None: False)

    lines = egress.render(egress.audit(_config()), offline=False)
    joined = "\n".join(lines)

    assert lines[0].startswith("egress posture: public-fetch")
    assert "× reranker (optional)" in joined
    assert "fix: SKYGREP_RERANK_MODEL=" in joined


def test_render_says_when_offline_is_actually_enforced(served):
    served.add("bge-m3:latest")
    lines = egress.render(egress.audit(_config(rerank_model="/tmp")), offline=True)

    assert "SKYGREP_OFFLINE enforced" in lines[0]
