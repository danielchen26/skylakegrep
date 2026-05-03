"""Tests for L2 symbol-aware indexing.

Covers four layers:
  1. ``extract_file_symbols`` returns the right names + kinds and uses the
     camelCase-split lower form.
  2. ``populate_symbols`` writes the right number of rows.
  3. ``symbol_match_boost`` boosts the file whose symbols match the query.
  4. End-to-end: a query that lexically misses but symbolically hits ranks
     the symbolic file top-1 through the CLI.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from local_mgrep.src import cli as cli_module
from local_mgrep.src.indexer import _split_camel_lower, extract_file_symbols
from local_mgrep.src.storage import (
    init_db,
    populate_symbols,
    search,
    store_chunks_batch,
    symbol_match_boost,
)


class FakeEmbedder:
    """Tiny embedder for end-to-end tests.

    The first dim signals "language model client" semantics; the second is
    pure noise. Two of the three test files share the same vector so the
    cosine score is identical, leaving the symbol boost as the tiebreaker.
    """

    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]


class ExtractFileSymbolsTests(unittest.TestCase):
    def test_python_camel_case_class_and_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            f = root / "client.py"
            f.write_text(
                "class LanguageModelClient:\n"
                "    def __init__(self, name):\n"
                "        self.name = name\n"
                "\n"
                "def fetch_user_token(user_id):\n"
                "    return user_id\n"
            )
            rows = extract_file_symbols(f, root)
        names = {r["name"]: r["kind"] for r in rows}
        self.assertIn("LanguageModelClient", names)
        self.assertEqual(names["LanguageModelClient"], "class")
        self.assertIn("fetch_user_token", names)
        self.assertEqual(names["fetch_user_token"], "function")

        # Camel-case split lower form.
        client_row = next(r for r in rows if r["name"] == "LanguageModelClient")
        self.assertEqual(client_row["name_lower"], "language model client")
        snake_row = next(r for r in rows if r["name"] == "fetch_user_token")
        self.assertEqual(snake_row["name_lower"], "fetch_user_token")

    def test_split_camel_lower_helper(self):
        self.assertEqual(_split_camel_lower("LanguageModelClient"), "language model client")
        self.assertEqual(_split_camel_lower("fetchUserToken"), "fetch user token")
        self.assertEqual(_split_camel_lower("snake_case_name"), "snake_case_name")
        self.assertEqual(_split_camel_lower("HTTPServer"), "httpserver")  # all-upper run stays joined

    def test_three_files_distinct_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alpha.py").write_text("def alpha_func():\n    return 1\n")
            (root / "beta.py").write_text("class BetaWidget:\n    pass\n")
            (root / "gamma.py").write_text("def gamma_helper():\n    pass\n\nclass GammaCore:\n    pass\n")
            alpha = extract_file_symbols(root / "alpha.py", root)
            beta = extract_file_symbols(root / "beta.py", root)
            gamma = extract_file_symbols(root / "gamma.py", root)
        self.assertEqual([(r["name"], r["kind"]) for r in alpha], [("alpha_func", "function")])
        self.assertEqual([(r["name"], r["kind"]) for r in beta], [("BetaWidget", "class")])
        gamma_pairs = sorted((r["name"], r["kind"]) for r in gamma)
        self.assertEqual(gamma_pairs, [("GammaCore", "class"), ("gamma_helper", "function")])


class PopulateSymbolsTests(unittest.TestCase):
    def test_populate_symbols_inserts_rows_for_indexed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = {
                "alpha.py": "def alpha_func():\n    return 1\n",
                "beta.py": "class BetaWidget:\n    pass\n",
                "gamma.py": "def gamma_helper():\n    pass\n\nclass GammaCore:\n    pass\n",
            }
            for name, body in files.items():
                (root / name).write_text(body)
            db_path = root / "index.db"
            conn = init_db(db_path)
            store_chunks_batch(
                conn,
                [
                    {
                        "file": str(root / name),
                        "chunk": body,
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 5,
                        "start_byte": 0,
                        "end_byte": len(body),
                        "embedding": [1.0, 0.0],
                    }
                    for name, body in files.items()
                ],
            )
            inserted = populate_symbols(conn, root)
            count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            # Expected: alpha=1, beta=1, gamma=2 → 4 rows.
            self.assertEqual(inserted, 4)
            self.assertEqual(count, 4)
            # Idempotent — running again must not double the row count.
            populate_symbols(conn, root)
            count2 = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            self.assertEqual(count2, 4)


class SymbolMatchBoostTests(unittest.TestCase):
    def test_boost_picks_camelcase_symbol_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "client.py").write_text(
                "class LanguageModelClient:\n"
                "    def call(self):\n"
                "        return 'hi'\n"
            )
            (root / "noise.py").write_text("def unrelated():\n    return 0\n")
            db_path = root / "index.db"
            conn = init_db(db_path)
            store_chunks_batch(
                conn,
                [
                    {
                        "file": str(root / "client.py"),
                        "chunk": "class LanguageModelClient:\n    def call(self):\n        return 'hi'\n",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 3,
                        "start_byte": 0,
                        "end_byte": 80,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": str(root / "noise.py"),
                        "chunk": "def unrelated():\n    return 0\n",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 30,
                        "embedding": [1.0, 0.0],
                    },
                ],
            )
            populate_symbols(conn, root)
            boosts = symbol_match_boost(conn, "language model client implementation")
        # client.py must be boosted; noise.py must not appear.
        client_path = str(root / "client.py")
        noise_path = str(root / "noise.py")
        self.assertIn(client_path, boosts)
        self.assertNotIn(noise_path, boosts)
        self.assertGreater(boosts[client_path], 0.0)


class EndToEndSymbolBoostTests(unittest.TestCase):
    def test_query_lexically_misses_symbolically_hits_ranks_top1(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # File A has the desired symbol but its body never literally
            # mentions "language" or "model" — only the camelCase symbol
            # name does. So a pure-lexical-on-chunk-text scorer cannot rank
            # it; the symbol boost must.
            (root / "client.py").write_text(
                "class LanguageModelClient:\n"
                "    def call(self):\n"
                "        return self._send()\n"
                "\n"
                "    def _send(self):\n"
                "        return None\n"
            )

            # File B has a tangentially-related body but no matching symbol.
            (root / "other.py").write_text(
                "def make_request():\n"
                "    payload = {'q': 'hello'}\n"
                "    return payload\n"
            )

            db_path = root / "index.db"
            conn = init_db(db_path)
            store_chunks_batch(
                conn,
                [
                    {
                        "file": str(root / "client.py"),
                        "chunk": "class LanguageModelClient:\n    def call(self):\n        return self._send()\n",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 3,
                        "start_byte": 0,
                        "end_byte": 80,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": str(root / "other.py"),
                        "chunk": "def make_request():\n    payload = {'q': 'hello'}\n    return payload\n",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 3,
                        "start_byte": 0,
                        "end_byte": 70,
                        "embedding": [1.0, 0.0],
                    },
                ],
            )
            populate_symbols(conn, root)

            # Direct ``search`` call: with use_symbol_boost the LanguageModel
            # file must lead.
            with_boost = search(
                conn,
                [1.0, 0.0],
                top_k=2,
                query_text="language model client",
                use_symbol_boost=True,
            )
            self.assertEqual(with_boost[0]["path"], str(root / "client.py"))
            self.assertGreater(with_boost[0].get("symbol_boost", 0.0), 0.0)

            # And via the CLI.
            runner = CliRunner()
            old_db_path = os.environ.get("MGREP_DB_PATH")
            os.environ["MGREP_DB_PATH"] = str(db_path)
            try:
                with patch.object(cli_module, "get_embedder", return_value=FakeEmbedder()):
                    result = runner.invoke(
                        cli_module.cli,
                        [
                            "search",
                            "--no-cascade",
                            "--no-lexical-prefilter",
                            "--no-rerank",
                            "--no-multi-resolution",
                            "--semantic-only",
                            "--json",
                            "-m",
                            "2",
                            "language model client",
                        ],
                    )
            finally:
                if old_db_path is None:
                    os.environ.pop("MGREP_DB_PATH", None)
                else:
                    os.environ["MGREP_DB_PATH"] = old_db_path
            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertGreater(len(payload), 0)
            self.assertEqual(payload[0]["path"], str(root / "client.py"))


if __name__ == "__main__":
    unittest.main()
