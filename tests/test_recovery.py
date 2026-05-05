"""Tests for the intelligent-recovery detection logic.

Regression coverage for the pre-0.2.2 index handling that 0.2.4 got
wrong: when a user upgrades skylakegrep from a version that
predates the metadata table (or simply ran ``init_db`` before any
embedder fingerprint was recorded), the chunks/vectors tables can
hold stale-dim vectors with no ``embedder_fingerprint`` key. The
0.2.5 fix is that ``detect_mismatch`` distinguishes between a
truly-empty fresh index (no fingerprint, no chunks → just stamp the
fingerprint) and a pre-fingerprint index with stale-dim chunks (no
fingerprint, stale chunks > 0 → trigger recovery).
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from skylakegrep.src.recovery import detect_mismatch
from skylakegrep.src.storage import init_db


class _MockEmbedder:
    """Minimal embedder stand-in for the fingerprint helper. ``model``
    matches what ``OllamaEmbedder`` exposes; ``current_dim`` is passed
    to ``detect_mismatch`` separately so we don't need a real
    embed-batch path."""

    def __init__(self, model: str = "bge-m3"):
        self.model = model


def _make_chunk_with_dim(conn, chunk_id: int, dim: int) -> None:
    """Insert one chunk + one zero-byte-blob vector of the given
    dimension. ``vstack``-safe (we never touch the values; the test
    only checks length()/4 == dim arithmetic in
    ``count_stale_chunks``)."""

    conn.execute(
        "INSERT INTO chunks (id, file, chunk, language, chunk_index) "
        "VALUES (?, ?, '', 'python', 0)",
        (chunk_id, f"file_{chunk_id}.py"),
    )
    conn.execute(
        "INSERT INTO vectors (id, embedding) VALUES (?, ?)",
        (chunk_id, b"\x00" * (4 * dim)),
    )


class DetectMismatchPre022IndexTests(unittest.TestCase):
    """The headline regression: pre-0.2.2 index with stale chunks +
    no fingerprint must trigger recovery, not silently stamp a
    matching fingerprint and leave the user invisible to the new
    embedder."""

    def _fresh_conn(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        return init_db(Path(tmp.name)), Path(tmp.name)

    def test_truly_empty_fresh_index_stamps_fingerprint_no_recovery(self):
        conn, path = self._fresh_conn()
        try:
            result = detect_mismatch(conn, _MockEmbedder(), current_dim=1024)
            self.assertIsNone(result, "fresh empty index should not trigger recovery")
            stored = conn.execute(
                "SELECT value FROM metadata WHERE key='embedder_fingerprint'"
            ).fetchone()
            self.assertIsNotNone(stored, "fingerprint should now be recorded")
            self.assertEqual(stored[0], "bge-m3:1024")
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_pre022_stale_chunks_trigger_recovery(self):
        """768-d chunks + no fingerprint + bge-m3:1024 query → mismatch."""

        conn, path = self._fresh_conn()
        try:
            for i in range(1, 6):
                _make_chunk_with_dim(conn, i, dim=768)
            conn.commit()
            result = detect_mismatch(conn, _MockEmbedder(), current_dim=1024)
            self.assertIsNotNone(
                result,
                "pre-0.2.2 stale-dim chunks must surface as a mismatch so "
                "the recovery worker can re-embed them; the 0.2.4 bug was "
                "that this returned None and left them invisible.",
            )
            self.assertEqual(result["stale_count"], 5)
            self.assertEqual(result["total_count"], 5)
            self.assertEqual(result["current_fingerprint"], "bge-m3:1024")
            self.assertEqual(result["stored_fingerprint"], "(unrecorded)")
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_pre022_chunks_already_at_current_dim_no_recovery(self):
        """1024-d chunks + no fingerprint + bge-m3:1024 query → no
        recovery; the absence of fingerprint just means the index was
        built with the same model but never recorded the metadata
        key."""

        conn, path = self._fresh_conn()
        try:
            for i in range(1, 4):
                _make_chunk_with_dim(conn, i, dim=1024)
            conn.commit()
            result = detect_mismatch(conn, _MockEmbedder(), current_dim=1024)
            self.assertIsNone(result)
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_recorded_fingerprint_match_no_recovery(self):
        """When the index records a fingerprint that matches the
        current embedder, we never touch the chunks — even if some
        rogue chunk happened to have a different dim. This is the
        common steady-state path."""

        conn, path = self._fresh_conn()
        try:
            conn.execute(
                "INSERT INTO metadata (key, value) VALUES "
                "('embedder_fingerprint', 'bge-m3:1024')"
            )
            conn.commit()
            result = detect_mismatch(conn, _MockEmbedder(), current_dim=1024)
            self.assertIsNone(result)
        finally:
            conn.close()
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
