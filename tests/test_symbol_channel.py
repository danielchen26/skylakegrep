# SPDX-License-Identifier: Apache-2.0
"""Tests for the symbol-as-retriever channel.

Verifies four properties:
  1. ``symbol_channel_search`` returns the file containing a query-named
     symbol even when the chunk text never literally mentions the term.
  2. The function silently returns ``[]`` when the query has no terms or
     the ``symbols`` table is empty.
  3. Exact-name matches outrank camelCase-token matches.
  4. ``multi_channel_search`` fuses the two channels and surfaces a
     symbol-only file that cosine alone would miss.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from skylakegrep.src.storage import (
    init_db,
    populate_symbols,
    store_chunks_batch,
)
from skylakegrep.src.symbol_channel import (
    multi_channel_search,
    symbol_channel_search,
)


class FakeEmbedder:
    """Two-dim embedder used so cosine alone never picks the right file.

    Both files get the same embedding ``[1, 0]``; the symbol channel is
    the only signal that can break the tie. Used in
    ``test_multi_channel_surfaces_symbol_only_file``.
    """

    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]


def _store_chunk(conn, root: Path, name: str, body: str, embedding: list[float] | None = None) -> str:
    """Helper: write ``name`` under ``root`` and store one chunk for it.

    Returns the chunk's ``file`` column value (absolute path string), so
    tests can build path expectations.
    """
    p = root / name
    p.write_text(body)
    file_str = str(p)
    store_chunks_batch(
        conn,
        [
            {
                "file": file_str,
                "chunk": body,
                "language": "python",
                "chunk_index": 0,
                "file_mtime": 1.0,
                "start_line": 1,
                "end_line": body.count("\n") or 1,
                "start_byte": 0,
                "end_byte": len(body),
                "embedding": embedding or [1.0, 0.0],
            }
        ],
    )
    return file_str


class SymbolChannelSearchTests(unittest.TestCase):
    def test_returns_file_with_matching_function_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client_path = _store_chunk(
                conn := init_db(root / "index.db"),
                root,
                "client.py",
                "class LanguageModelClient:\n    def call(self):\n        return 1\n",
            )
            _store_chunk(
                conn,
                root,
                "noise.py",
                "def unrelated():\n    return 0\n",
            )
            populate_symbols(conn, root)
            results = symbol_channel_search(conn, "language model client", top_k=5)
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["path"], client_path)
        self.assertTrue(results[0]["symbol_channel"])
        self.assertIn("language", results[0]["symbol_channel_terms"])

    def test_silent_fallback_when_no_terms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = init_db(root / "index.db")
            _store_chunk(conn, root, "client.py", "def foo():\n    return 1\n")
            populate_symbols(conn, root)
            # All-stopwords query — no ≥4-char salient term survives.
            results = symbol_channel_search(conn, "the a an is to of", top_k=5)
        self.assertEqual(results, [])

    def test_silent_fallback_when_symbols_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = init_db(root / "index.db")
            # Chunk inserted but populate_symbols never run.
            _store_chunk(conn, root, "client.py", "class LanguageModelClient: pass\n")
            results = symbol_channel_search(conn, "language model client", top_k=5)
        self.assertEqual(results, [])

    def test_exact_name_wins_over_token_split(self):
        """An exact-name match outranks a camelCase-split token match.

        ``createElement`` exists as a literal function in file A.
        File B has ``CreateElementFactory`` whose camelCase split also
        contains ``element``, but the *exact* match should win.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = init_db(root / "index.db")
            exact_path = _store_chunk(
                conn,
                root,
                "exact.py",
                "def createElement(name):\n    return name\n",
            )
            _store_chunk(
                conn,
                root,
                "factory.py",
                "class CreateElementFactory:\n    def build(self):\n        return 1\n",
            )
            populate_symbols(conn, root)
            results = symbol_channel_search(conn, "createElement implementation", top_k=5)
        # Both files match (one via exact, one via camelCase token), but
        # exact must come first.
        self.assertGreaterEqual(len(results), 1)
        self.assertEqual(results[0]["path"], exact_path)
        self.assertTrue(results[0].get("symbol_channel_exact"))


class MultiChannelSearchTests(unittest.TestCase):
    def test_multi_channel_surfaces_symbol_only_file(self):
        """Symbol channel must rescue a file cosine would miss.

        Both files share the same embedding ``[1,0]`` so cosine alone
        is a coin flip; only the symbol channel disambiguates.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = init_db(root / "index.db")
            client_path = _store_chunk(
                conn,
                root,
                "client.py",
                "class LanguageModelClient:\n    def call(self):\n        return 1\n",
            )
            other_path = _store_chunk(
                conn,
                root,
                "other.py",
                "def make_request():\n    return None\n",
            )
            populate_symbols(conn, root)
            results, telem = multi_channel_search(
                conn,
                "language model client",
                embedder=FakeEmbedder(),
                top_k=2,
                cosine_pool=5,
                symbol_pool=5,
                rerank=False,
            )
        self.assertGreater(len(results), 0)
        self.assertEqual(results[0]["path"], client_path)
        # Telemetry sanity.
        self.assertGreaterEqual(telem["symbol_n"], 1)
        self.assertGreaterEqual(telem["hits_in_symbol_channel"], 1)
        self.assertIn("cosine_ms", telem)
        self.assertIn("symbol_ms", telem)
        self.assertIn("fuse_ms", telem)


if __name__ == "__main__":
    unittest.main()
