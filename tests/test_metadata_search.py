# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import time

from skylakegrep.src.metadata_search import (
    analyze_metadata_query,
    classify_metadata_query,
    descriptor_file_results,
    metadata_results,
    rank_results_by_metadata,
)


def test_latest_opened_files_are_metadata_query():
    q = classify_metadata_query("show the 4 most recently opened files")
    assert q is not None
    assert q.kind == "opened"
    assert q.limit == 4


def test_latest_implementation_is_not_metadata_query():
    assert classify_metadata_query(
        "how does the latest implementation handle errors"
    ) is None


def test_recently_created_specific_artifact_is_not_metadata_only():
    assert classify_metadata_query(
        "show me where my project brief that I recently created "
        "in PROJECT folder"
    ) is None


def test_recently_created_specific_artifact_is_metadata_modifier():
    facet = analyze_metadata_query(
        "show me where my project brief that I recently created "
        "in PROJECT folder"
    )
    assert facet is not None
    assert facet.kind == "created"
    assert facet.terminal is False
    assert "brief" in facet.target_descriptors


def test_metadata_terminal_ignores_explicit_scope_label():
    q = classify_metadata_query("show recently created files in CASE42 folder")
    assert q is not None
    assert q.kind == "created"


def test_latest_files_with_content_descriptor_is_not_unfiltered_metadata():
    assert classify_metadata_query("latest python files") is None


def test_cjk_metadata_only_vs_metadata_modifier():
    terminal = analyze_metadata_query("最近打开过的文件")
    assert terminal is not None
    assert terminal.kind == "opened"
    assert terminal.terminal is True

    modifier = analyze_metadata_query("我最近打开过的合同在哪")
    assert modifier is not None
    assert modifier.kind == "opened"
    assert modifier.terminal is False
    assert "合同" in modifier.target_descriptors


def test_code_identifier_created_at_is_not_metadata_facet():
    assert analyze_metadata_query("how does created_at field work") is None
    assert analyze_metadata_query("where is created_at file") is None


def test_metadata_results_sort_by_opened_time(tmp_path):
    older = tmp_path / "older.txt"
    newer = tmp_path / "newer.txt"
    older.write_text("older\n")
    newer.write_text("newer\n")
    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now - 50))

    results, meta = metadata_results(
        "latest 2 files i opened",
        tmp_path,
        top_k=5,
    )

    assert meta is not None
    assert meta.kind == "opened"
    assert [r["path"] for r in results[:2]] == [str(newer), str(older)]
    assert all(r["fallback"] == "metadata-opened" for r in results)


def test_metadata_results_sort_by_created_time(tmp_path):
    older = tmp_path / "older.txt"
    newer = tmp_path / "newer.txt"
    older.write_text("older\n")
    newer.write_text("newer\n")
    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))

    results, meta = metadata_results(
        "latest 2 recently created files",
        tmp_path,
        top_k=5,
    )

    assert meta is not None
    assert meta.kind == "created"
    assert [r["path"] for r in results[:2]] == [str(newer), str(older)]
    assert all(r["fallback"] == "metadata-created" for r in results)


def test_metadata_modifier_reranks_existing_relevant_results(tmp_path):
    older = tmp_path / "report_old.txt"
    newer = tmp_path / "report_new.txt"
    older.write_text("report\n")
    newer.write_text("report\n")
    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))
    results = [
        {"path": str(older), "score": 0.5},
        {"path": str(newer), "score": 0.5},
    ]

    ranked = rank_results_by_metadata(results, "modified")

    assert [r["path"] for r in ranked] == [str(newer), str(older)]


def test_metadata_results_do_not_return_unfiltered_composite_matches(tmp_path):
    (tmp_path / "unrelated.txt").write_text("unrelated\n")

    results, meta = metadata_results(
        "show me where my project brief that I recently created "
        "in PROJECT folder",
        tmp_path,
        top_k=5,
    )

    assert meta is None
    assert results == []


def test_descriptor_file_results_constrain_metadata_modifier_by_target(tmp_path):
    scoped = tmp_path / "CASE42"
    scoped.mkdir()
    wanted = scoped / "project_brief.pdf"
    generated = scoped / "project_brief.blg"
    other = scoped / "unrelated_notes.pdf"
    wanted.write_text("brief\n")
    generated.write_text("generated\n")
    other.write_text("notes\n")
    now = time.time()
    os.utime(wanted, (now, now))
    os.utime(other, (now + 10, now + 10))

    results, facet = descriptor_file_results(
        "show me where my project brief that I recently created "
        "in CASE42 folder",
        scoped,
        top_k=5,
    )

    assert facet is not None
    assert facet.kind == "created"
    assert facet.terminal is False
    assert [os.path.basename(r["path"]) for r in results] == ["project_brief.pdf"]
    assert results[0]["fallback"] == "metadata-descriptor-created"
