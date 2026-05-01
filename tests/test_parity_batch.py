import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from local_mgrep.src import cli as cli_module
from local_mgrep.src.indexer import batch_embed, collect_indexable_files
from local_mgrep.src.storage import init_db, store_chunks_batch


class KeywordEmbedder:
    def embed(self, text: str) -> list[float]:
        lowered = text.lower()
        if "refresh" in lowered:
            return [0.8, 0.2]
        if "ui" in lowered or "login" in lowered:
            return [0.0, 1.0]
        return [1.0, 0.0]


class RecordingAgenticAnswerer:
    def __init__(self):
        self.decompose_calls = []

    def decompose(self, query: str, max_queries: int = 3) -> list[str]:
        self.decompose_calls.append((query, max_queries))
        return ["validate token", "refresh token", "ui login", "extra ignored query"]

    def answer(self, query: str, results: list[dict]) -> str:
        return "Local answer"


class BatchOnlyEmbedder:
    def __init__(self):
        self.calls = []

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [[float(index), 0.0] for index, _ in enumerate(texts, start=1)]


def with_db_path(db_path: Path):
    class DbPathContext:
        def __enter__(self):
            self.old_db_path = os.environ.get("MGREP_DB_PATH")
            os.environ["MGREP_DB_PATH"] = str(db_path)

        def __exit__(self, exc_type, exc, tb):
            if self.old_db_path is None:
                os.environ.pop("MGREP_DB_PATH", None)
            else:
                os.environ["MGREP_DB_PATH"] = self.old_db_path

    return DbPathContext()


class ParityBatchTests(unittest.TestCase):
    def test_collect_indexable_files_honors_mgrepignore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".mgrepignore").write_text("secret.py\nlocal-only/\n", encoding="utf-8")
            (root / "kept.py").write_text("print('kept')\n", encoding="utf-8")
            (root / "secret.py").write_text("print('secret')\n", encoding="utf-8")
            (root / "local-only").mkdir()
            (root / "local-only" / "ignored.py").write_text("print('ignored')\n", encoding="utf-8")

            files = {path.relative_to(root).as_posix() for path in collect_indexable_files(root)}

        self.assertEqual(files, {"kept.py"})

    def test_incremental_index_removes_deleted_files_from_search(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "repo"
            root.mkdir()
            db_path = Path(temp_dir) / "index.db"
            target = root / "auth.py"
            target.write_text("def deleted_token_symbol():\n    return 'token'\n", encoding="utf-8")
            runner = CliRunner()

            with with_db_path(db_path):
                with patch.object(cli_module, "get_embedder", return_value=KeywordEmbedder()):
                    first = runner.invoke(cli_module.cli, ["index", str(root), "--reset"])
                    self.assertEqual(first.exit_code, 0, first.output)
                    target.unlink()
                    second = runner.invoke(cli_module.cli, ["index", str(root)])
                    self.assertEqual(second.exit_code, 0, second.output)
                    result = runner.invoke(cli_module.cli, ["search", "deleted token symbol", "--json"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.output), [])

    def test_search_supports_m_count_language_and_path_filters(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "index.db"
            conn = init_db(db_path)
            store_chunks_batch(
                conn,
                [
                    {
                        "file": "src/auth.py",
                        "chunk": "def validate_token(token):\n    return token.startswith('token-')",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 50,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": "src/auth_test.py",
                        "chunk": "def test_validate_token():\n    assert True",
                        "language": "python",
                        "chunk_index": 1,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 40,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": "web/login.ts",
                        "chunk": "export function loginUi() { return 'ui' }",
                        "language": "typescript",
                        "chunk_index": 2,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 1,
                        "start_byte": 0,
                        "end_byte": 40,
                        "embedding": [0.0, 1.0],
                    },
                ],
            )
            runner = CliRunner()
            with with_db_path(db_path):
                with patch.object(cli_module, "get_embedder", return_value=KeywordEmbedder()):
                    result = runner.invoke(
                        cli_module.cli,
                        [
                            "search",
                            "token",
                            "--json",
                            "-m",
                            "1",
                            "--language",
                            "python",
                            "--include",
                            "src/*",
                            "--exclude",
                            "*_test.py",
                        ],
                    )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["path"], "src/auth.py")

    def test_search_no_content_suppresses_snippets_in_human_output(self):
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
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 50,
                        "embedding": [1.0, 0.0],
                    }
                ],
            )
            runner = CliRunner()
            with with_db_path(db_path):
                with patch.object(cli_module, "get_embedder", return_value=KeywordEmbedder()):
                    result = runner.invoke(cli_module.cli, ["search", "token", "--no-content"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("auth.py:1-2", result.output)
        self.assertNotIn("def validate_token", result.output)

    def test_agentic_search_uses_bounded_local_subqueries(self):
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
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 50,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": "refresh.py",
                        "chunk": "def refresh_token(token):\n    return token + '-new'",
                        "language": "python",
                        "chunk_index": 1,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 50,
                        "embedding": [0.8, 0.2],
                    },
                ],
            )
            answerer = RecordingAgenticAnswerer()
            runner = CliRunner()
            with with_db_path(db_path):
                with patch.object(cli_module, "get_embedder", return_value=KeywordEmbedder()):
                    with patch.object(cli_module, "get_answerer", return_value=answerer):
                        result = runner.invoke(cli_module.cli, ["search", "token lifecycle", "--agentic", "--json", "-m", "5"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(answerer.decompose_calls, [("token lifecycle", 3)])
        payload = json.loads(result.output)
        paths = {entry["path"] for entry in payload}
        self.assertIn("auth.py", paths)
        self.assertIn("refresh.py", paths)

    def test_batch_embed_uses_embedder_batch_api_for_indexing_speed(self):
        chunks = [
            {"chunk": "one", "embedding": None},
            {"chunk": "two", "embedding": None},
            {"chunk": "three", "embedding": None},
        ]
        embedder = BatchOnlyEmbedder()

        embedded = batch_embed(chunks, embedder, batch_size=2)

        self.assertEqual(embedder.calls, [["one", "two"], ["three"]])
        self.assertEqual([item["embedding"] for item in embedded], [[1.0, 0.0], [2.0, 0.0], [1.0, 0.0]])


if __name__ == "__main__":
    unittest.main()
