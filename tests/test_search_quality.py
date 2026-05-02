import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from local_mgrep.src import cli as cli_module
from local_mgrep.src.indexer import collect_indexable_files, prepare_file_chunks
from local_mgrep.src.storage import init_db, search, store_chunks_batch


class StaticEmbedder:
    def embed(self, text: str) -> list[float]:
        if "token" in text.lower():
            return [1.0, 0.0]
        return [0.0, 1.0]


class SearchQualityTests(unittest.TestCase):
    def test_prepare_file_chunks_records_line_ranges_when_parser_falls_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "main.go"
            path.write_text(
                "package main\n\n"
                "func greet() {\n"
                "    println(\"hello\")\n"
                "}\n",
                encoding="utf-8",
            )

            with patch("local_mgrep.src.indexer.get_parser", return_value=None):
                chunks = prepare_file_chunks(path)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["start_line"], 1)
        self.assertEqual(chunks[0]["end_line"], 5)
        self.assertEqual(chunks[0]["start_byte"], 0)
        self.assertGreater(chunks[0]["end_byte"], chunks[0]["start_byte"])

    def test_collect_indexable_files_honors_gitignore_and_default_vendor_dirs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".gitignore").write_text("ignored.py\nbuild/\n", encoding="utf-8")
            (root / "kept.py").write_text("print('kept')\n", encoding="utf-8")
            (root / "ignored.py").write_text("print('ignored')\n", encoding="utf-8")
            (root / "build").mkdir()
            (root / "build" / "generated.py").write_text("print('generated')\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "vendor.py").write_text("print('vendor')\n", encoding="utf-8")

            files = {path.relative_to(root).as_posix() for path in collect_indexable_files(root)}

        self.assertEqual(files, {"kept.py"})

    def test_search_deduplicates_same_logical_result_and_returns_provenance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = init_db(Path(temp_dir) / "index.db")
            store_chunks_batch(
                conn,
                [
                    {
                        "file": "auth.py",
                        "chunk": "def validate_token(token):\n    return token.startswith('token-')",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 10,
                        "end_line": 11,
                        "start_byte": 100,
                        "end_byte": 160,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": "auth.py",
                        "chunk": "def validate_token(token):\n    return token.startswith('token-')",
                        "language": "python",
                        "chunk_index": 1,
                        "file_mtime": 1.0,
                        "start_line": 10,
                        "end_line": 11,
                        "start_byte": 100,
                        "end_byte": 160,
                        "embedding": [1.0, 0.0],
                    },
                ],
            )

            results = search(conn, [1.0, 0.0], top_k=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["path"], "auth.py")
        self.assertEqual(results[0]["start_line"], 10)
        self.assertEqual(results[0]["end_line"], 11)
        self.assertIn("validate_token", results[0]["snippet"])

    def test_search_json_outputs_stable_result_schema(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "index.db"
            conn = init_db(db_path)
            store_chunks_batch(
                conn,
                [
                    {
                        "file": "auth.py",
                        "chunk": "def validate_token(token):\n    return token.startswith('token-')",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 3,
                        "end_line": 4,
                        "start_byte": 20,
                        "end_byte": 80,
                        "embedding": [1.0, 0.0],
                    }
                ],
            )
            runner = CliRunner()
            old_db_path = os.environ.get("MGREP_DB_PATH")
            os.environ["MGREP_DB_PATH"] = str(db_path)
            try:
                with patch.object(cli_module, "get_embedder", return_value=StaticEmbedder()):
                    result = runner.invoke(cli_module.cli, ["search", "token", "--json", "--no-rerank"])
            finally:
                if old_db_path is None:
                    os.environ.pop("MGREP_DB_PATH", None)
                else:
                    os.environ["MGREP_DB_PATH"] = old_db_path

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(
            sorted(payload[0].keys()),
            ["end_line", "language", "path", "score", "snippet", "start_line"],
        )
        self.assertEqual(payload[0]["path"], "auth.py")
        self.assertEqual(payload[0]["start_line"], 3)


if __name__ == "__main__":
    unittest.main()
