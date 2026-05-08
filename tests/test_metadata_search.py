from __future__ import annotations

import os
import time

from skylakegrep.src.metadata_search import (
    classify_metadata_query,
    metadata_results,
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
