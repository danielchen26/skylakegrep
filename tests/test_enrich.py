"""Tests for L3 doc2query chunk enrichment.

The LLM call (``requests.post``) is patched everywhere so the test runs
without an Ollama server. Patches return a deterministic response shaped
like the real Ollama generate API: ``{"response": "..."}``. The
embedder is a tiny ``KeywordEmbedder`` that returns a vector that varies
with the input text, so the post-enrichment vector blob is provably
different from the pre-enrichment blob.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_mgrep.src import enrich as enrich_mod
from local_mgrep.src.storage import init_db, store_chunks_batch


class KeywordEmbedder:
    """Deterministic two-dim embedder used in tests.

    The vector swings on whether the text contains the substring
    ``"description"`` (which the patched LLM always emits). That gives us
    a cheap way to assert the embedding actually changes when enrichment
    runs — different bytes pre vs post.
    """

    def embed(self, text: str) -> list[float]:
        # Length-aware so even chunks without 'description' get a unique
        # vector — guarantees pre/post bytes differ.
        if "description" in text.lower():
            return [0.7, 0.3, float(len(text)) / 1000.0]
        return [1.0, 0.0, float(len(text)) / 1000.0]


class _Resp:
    """Minimal stand-in for the ``requests.Response`` shape we read."""

    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _seed_db(db_path: Path) -> sqlite3.Connection:
    conn = init_db(db_path)
    store_chunks_batch(
        conn,
        [
            {
                "file": "auth.py",
                "chunk": "def login(user): return token",
                "language": "python",
                "chunk_index": 0,
                "file_mtime": 1.0,
                "start_line": 1,
                "end_line": 1,
                "start_byte": 0,
                "end_byte": 30,
                "embedding": [1.0, 0.0, 0.0],
            },
            {
                "file": "billing.py",
                "chunk": "def charge_card(amount): pass",
                "language": "python",
                "chunk_index": 0,
                "file_mtime": 1.0,
                "start_line": 1,
                "end_line": 1,
                "start_byte": 0,
                "end_byte": 30,
                "embedding": [1.0, 0.0, 0.0],
            },
            {
                "file": "logger.py",
                "chunk": "def log(msg): print(msg)",
                "language": "python",
                "chunk_index": 0,
                "file_mtime": 1.0,
                "start_line": 1,
                "end_line": 1,
                "start_byte": 0,
                "end_byte": 25,
                "embedding": [1.0, 0.0, 0.0],
            },
        ],
    )
    return conn


def _path_aware_post(*args, **kwargs):
    """Stand-in for ``requests.post`` returning a per-path description.

    The prompt includes ``File: <path>`` so we extract that and echo it
    back — this lets the test assert each chunk picked up its own
    description rather than a globally shared one.
    """
    body = kwargs.get("json") or {}
    prompt = body.get("prompt", "")
    path = "unknown"
    for line in prompt.splitlines():
        if line.startswith("File: "):
            path = line[len("File: "):].strip()
            break
    return _Resp({"response": f"description for {path}"})


class EnrichTests(unittest.TestCase):
    def test_enriches_all_pending_chunks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "index.db"
            conn = _seed_db(db_path)

            # Snapshot the pre-enrichment vectors so we can prove the
            # blobs were rewritten (resume invariant relies on this).
            pre = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT id, embedding FROM vectors"
                ).fetchall()
            }

            with patch(
                "local_mgrep.src.enrich.requests.post",
                side_effect=_path_aware_post,
            ):
                n = enrich_mod.enrich_pending_chunks(
                    conn,
                    embedder=KeywordEmbedder(),
                    quiet=True,
                )

        self.assertEqual(n, 3)
        rows = conn.execute(
            "SELECT file, description, enriched_at FROM chunks ORDER BY id"
        ).fetchall()
        self.assertEqual(len(rows), 3)
        for file_path, description, enriched_at in rows:
            self.assertIsNotNone(enriched_at, f"{file_path} not stamped")
            self.assertIsNotNone(description, f"{file_path} missing description")
            self.assertIn(file_path, description)

        post = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT id, embedding FROM vectors"
            ).fetchall()
        }
        for chunk_id, blob in post.items():
            self.assertNotEqual(
                pre[chunk_id], blob, f"vector for chunk {chunk_id} was not rewritten"
            )

        # No more candidates left — the pending set is exhausted.
        self.assertEqual(enrich_mod.count_pending(conn), 0)

    def test_resume_picks_up_remaining_chunks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "index.db"
            conn = _seed_db(db_path)

            with patch(
                "local_mgrep.src.enrich.requests.post",
                side_effect=_path_aware_post,
            ):
                first = enrich_mod.enrich_pending_chunks(
                    conn,
                    embedder=KeywordEmbedder(),
                    max_chunks=1,
                    quiet=True,
                )
            self.assertEqual(first, 1)
            self.assertEqual(enrich_mod.count_pending(conn), 2)

            with patch(
                "local_mgrep.src.enrich.requests.post",
                side_effect=_path_aware_post,
            ):
                second = enrich_mod.enrich_pending_chunks(
                    conn,
                    embedder=KeywordEmbedder(),
                    quiet=True,
                )
            # Resume picks up exactly the remaining 2 chunks — proves
            # commit-per-chunk persisted the first run's stamp.
            self.assertEqual(second, 2)
            self.assertEqual(enrich_mod.count_pending(conn), 0)

    def test_failed_llm_call_leaves_chunk_pending(self):
        """Resume invariant: when the LLM fails we keep ``enriched_at``
        NULL so the chunk re-enters the candidate set on the next pass."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "index.db"
            conn = _seed_db(db_path)

            import requests as _requests

            def boom(*args, **kwargs):
                raise _requests.RequestException("ollama down")

            with patch("local_mgrep.src.enrich.requests.post", side_effect=boom):
                n = enrich_mod.enrich_pending_chunks(
                    conn,
                    embedder=KeywordEmbedder(),
                    quiet=True,
                )
        self.assertEqual(n, 0)
        self.assertEqual(enrich_mod.count_pending(conn), 3)

    def test_count_enriched_progress(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "index.db"
            conn = _seed_db(db_path)
            self.assertEqual(enrich_mod.count_enriched(conn), (0, 3))
            with patch(
                "local_mgrep.src.enrich.requests.post",
                side_effect=_path_aware_post,
            ):
                enrich_mod.enrich_pending_chunks(
                    conn,
                    embedder=KeywordEmbedder(),
                    max_chunks=2,
                    quiet=True,
                )
            self.assertEqual(enrich_mod.count_enriched(conn), (2, 3))


if __name__ == "__main__":
    unittest.main()
