# SPDX-License-Identifier: Apache-2.0
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from skylakegrep.src import cli as cli_module
from skylakegrep.src.answerer import OllamaAnswerer
from skylakegrep.src.storage import init_db, store_chunks_batch


class StaticEmbedder:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]


class RecordingAnswerer:
    def __init__(self):
        self.calls = []

    def answer(self, query: str, results: list[dict]) -> str:
        self.calls.append((query, results))
        return "Token validation is implemented in auth.py."


class AnswerModeTests(unittest.TestCase):
    def test_answer_inventory_is_complete_and_generation_is_deterministic(self):
        response = MagicMock()
        response.json.return_value = {"response": "Complete answer"}
        response.raise_for_status.return_value = None
        answerer = OllamaAnswerer("http://localhost:11434", "test-model")

        with patch("skylakegrep.src.answerer.requests.post", return_value=response) as post:
            actual = answerer.answer(
                "What checks are required?",
                [
                    {
                        "path": "docs/RELEASING.md",
                        "start_line": 1,
                        "end_line": 2,
                        "snippet": "1. Run tests.\n2. Scan artifacts.",
                        "supporting_chunks": [
                            {
                                "path": "docs/RELEASING.md",
                                "start_line": 10,
                                "end_line": 11,
                                "snippet": "3. Verify public surfaces.",
                            }
                        ],
                    }
                ],
            )

        self.assertEqual(actual, "Complete answer")
        payload = post.call_args.kwargs["json"]
        self.assertIn("inventory every distinct fact or step", payload["prompt"])
        self.assertIn("include every distinct supported item", payload["prompt"])
        self.assertIn("[1.1] docs/RELEASING.md:10-11", payload["prompt"])
        self.assertIn("Verify public surfaces", payload["prompt"])
        self.assertEqual(
            payload["options"],
            {"temperature": 0, "seed": 42, "num_predict": 512},
        )

    def test_search_answer_synthesizes_from_local_results_with_sources(self):
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
            answerer = RecordingAnswerer()
            runner = CliRunner()
            old_db_path = os.environ.get("SKYGREP_DB_PATH")
            os.environ["SKYGREP_DB_PATH"] = str(db_path)
            try:
                with patch.object(cli_module, "get_embedder", return_value=StaticEmbedder()):
                    with patch.object(cli_module, "get_answerer", return_value=answerer):
                        result = runner.invoke(cli_module.cli, ["search", "--no-lexical-prefilter", "token validation", "--answer"])
            finally:
                if old_db_path is None:
                    os.environ.pop("SKYGREP_DB_PATH", None)
                else:
                    os.environ["SKYGREP_DB_PATH"] = old_db_path

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(answerer.calls[0][0], "token validation")
        self.assertEqual(answerer.calls[0][1][0]["path"], "auth.py")
        self.assertIn("Token validation is implemented", result.output)
        self.assertIn("Sources:", result.output)
        self.assertIn("auth.py:3-4", result.output)


if __name__ == "__main__":
    unittest.main()
