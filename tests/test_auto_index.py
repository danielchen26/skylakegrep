# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from skylakegrep.src import auto_index
from skylakegrep.src.storage import init_db


def test_incremental_refresh_defers_large_foreground_work(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    for i in range(3):
        (root / f"changed_{i}.txt").write_text(f"changed {i}\n")
    db_path = tmp_path / "index.db"
    conn = init_db(db_path)

    def _should_not_embed(*args, **kwargs):
        raise AssertionError("large refresh should be deferred before embedding")

    monkeypatch.setattr(auto_index, "get_embedder", _should_not_embed)

    refreshed = auto_index.incremental_refresh(
        conn,
        root,
        throttle_seconds=0,
        quiet=True,
        max_foreground_files=1,
    )

    assert refreshed == -3
    assert auto_index.index_status(conn)["chunks"] == 0
