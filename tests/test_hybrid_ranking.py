import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from local_mgrep.src import cli as cli_module
from local_mgrep.src.storage import init_db, search, store_chunks_batch


class SemanticOnlyEmbedder:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]


class HybridRankingTests(unittest.TestCase):
    def test_hybrid_search_boosts_exact_code_terms_over_semantic_only_score(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = init_db(Path(temp_dir) / "index.db")
            store_chunks_batch(
                conn,
                [
                    {
                        "file": "semantic.py",
                        "chunk": "def unrelated_name(value):\n    return value",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 40,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": "lexical.py",
                        "chunk": "def refresh_token(token):\n    return token + '-new'",
                        "language": "python",
                        "chunk_index": 1,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 50,
                        "embedding": [0.9, 0.1],
                    },
                ],
            )

            results = search(conn, [1.0, 0.0], top_k=2, query_text="refresh token")

        self.assertEqual(results[0]["path"], "lexical.py")
        self.assertGreater(results[0]["lexical_score"], 0.0)
        self.assertGreater(results[0]["score"], results[0]["semantic_score"])

    def test_semantic_only_flag_disables_lexical_boosting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "index.db"
            conn = init_db(db_path)
            store_chunks_batch(
                conn,
                [
                    {
                        "file": "semantic.py",
                        "chunk": "def unrelated_name(value):\n    return value",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 40,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": "lexical.py",
                        "chunk": "def refresh_token(token):\n    return token + '-new'",
                        "language": "python",
                        "chunk_index": 1,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 50,
                        "embedding": [0.9, 0.1],
                    },
                ],
            )
            runner = CliRunner()
            old_db_path = os.environ.get("MGREP_DB_PATH")
            os.environ["MGREP_DB_PATH"] = str(db_path)
            try:
                with patch.object(cli_module, "get_embedder", return_value=SemanticOnlyEmbedder()):
                    result = runner.invoke(cli_module.cli, ["search", "refresh token", "--semantic-only", "--json", "-m", "2", "--no-rerank"])
            finally:
                if old_db_path is None:
                    os.environ.pop("MGREP_DB_PATH", None)
                else:
                    os.environ["MGREP_DB_PATH"] = old_db_path

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload[0]["path"], "semantic.py")

    def test_search_diversifies_results_before_repeating_same_file(self):
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
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 60,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": "auth.py",
                        "chunk": "def refresh_token(token):\n    return token + '-new'",
                        "language": "python",
                        "chunk_index": 1,
                        "file_mtime": 1.0,
                        "start_line": 5,
                        "end_line": 6,
                        "start_byte": 100,
                        "end_byte": 150,
                        "embedding": [0.99, 0.01],
                    },
                    {
                        "file": "auth.py",
                        "chunk": "def revoke_token(token):\n    return token",
                        "language": "python",
                        "chunk_index": 2,
                        "file_mtime": 1.0,
                        "start_line": 9,
                        "end_line": 10,
                        "start_byte": 180,
                        "end_byte": 220,
                        "embedding": [0.98, 0.02],
                    },
                    {
                        "file": "session.py",
                        "chunk": "def create_session(user):\n    return {'token': user.id}",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 60,
                        "embedding": [0.97, 0.03],
                    },
                ],
            )

            results = search(conn, [1.0, 0.0], top_k=3, semantic_only=True)

        self.assertEqual([result["path"] for result in results], ["auth.py", "auth.py", "session.py"])


if __name__ == "__main__":
    unittest.main()
