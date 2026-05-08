"""Tests for the 0.4.0 UX changes: bare-form routing, doctor, default flips."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from skylakegrep.src import cli as cli_module
from skylakegrep.src import config as config_module


class BareFormRoutingTests(unittest.TestCase):
    """``skygrep "<query>"`` should route to ``search`` automatically."""

    def test_unknown_first_arg_routes_to_search(self):
        # Verified by parsing args through MgrepCLI.parse_args directly: any
        # non-flag, non-subcommand first token gets prepended with ``search``.
        ctx = cli_module.cli.make_context(
            "skygrep", [], resilient_parsing=True
        )
        # Re-parse through the custom group; expect args to be rewritten.
        args_in = ["a sample query"]
        rewritten = cli_module.cli.parse_args(ctx, list(args_in))
        # parse_args returns leftover args list; under our routing the
        # leftover will be the original token because ``search`` consumed it.
        # We check the side effect: ctx.protected_args + ctx.args together
        # should now begin with 'search'.
        full = ctx.protected_args + ctx.args
        self.assertTrue(full and full[0] == "search", full)

    def test_known_subcommand_does_not_route(self):
        ctx = cli_module.cli.make_context("skygrep", [], resilient_parsing=True)
        cli_module.cli.parse_args(ctx, ["doctor"])
        full = ctx.protected_args + ctx.args
        self.assertEqual(full[:1], ["doctor"])

    def test_help_flag_does_not_route_to_search(self):
        runner = CliRunner()
        result = runner.invoke(cli_module.cli, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Common usage", result.output)


class DoctorTests(unittest.TestCase):
    def test_doctor_reports_missing_ollama(self):
        runner = CliRunner()
        with patch("skylakegrep.src.bootstrap._probe_ollama", return_value=(False, "connection refused")):
            result = runner.invoke(cli_module.cli, ["doctor"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("ollama is required", result.output.lower())
        self.assertIn("connection refused", result.output.lower())

    def test_doctor_reports_present_models(self):
        runner = CliRunner()
        with patch("skylakegrep.src.bootstrap._probe_ollama", return_value=(True, "")):
            with patch(
                "skylakegrep.src.bootstrap.list_local_models",
                return_value=["bge-m3:latest", "qwen2.5:3b"],
            ):
                result = runner.invoke(cli_module.cli, ["doctor"])
        self.assertEqual(result.exit_code, 0, result.output)
        # The current default embed model is bge-m3 (BAAI's content-agnostic
        # general-purpose embedder); doctor output must reflect it.
        self.assertIn("bge-m3", result.output)


class ProjectRootTests(unittest.TestCase):
    def test_project_root_uses_git_toplevel(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            os.system(f"cd {root} && git init -q")
            sub = root / "a" / "b"
            sub.mkdir(parents=True)
            self.assertEqual(config_module.project_root(sub), root)

    def test_project_root_falls_back_to_cwd(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir).resolve()
            self.assertEqual(config_module.project_root(base), base)

    def test_project_db_path_is_deterministic_per_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir).resolve()
            a = config_module.project_db_path(base)
            b = config_module.project_db_path(base)
            self.assertEqual(a, b)
            other = config_module.project_db_path(base / "child" if (base / "child").exists() else base.parent)
            self.assertNotEqual(a, other)


class AutoIndexPolicyTests(unittest.TestCase):
    def test_resolve_db_path_respects_env_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            override = Path(temp_dir) / "x.db"
            with patch.dict(os.environ, {"SKYGREP_DB_PATH": str(override)}, clear=False):
                self.assertEqual(config_module.resolve_db_path(), override)

    def test_resolve_db_path_uses_project_scoped_default(self):
        env = dict(os.environ)
        env.pop("SKYGREP_DB_PATH", None)
        with patch.dict(os.environ, env, clear=True):
            path = config_module.resolve_db_path()
            self.assertIn(".skylakegrep/repos", str(path))


class SearchRoutingRegressionTests(unittest.TestCase):
    def test_high_confidence_filename_skip_cascade_keeps_json_path_alive(self):
        """Regression for the filename skip path: ``queries`` used to be
        initialised only inside the cascade branch, but the warm cross-folder
        gate read it after an LLM-authorised skip."""

        from skylakegrep.src.storage import init_db, store_chunks_batch

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            indexed_file = root / "dummy.py"
            indexed_file.write_text("print('indexed')\n")
            db_path = Path(temp_dir) / "index.db"
            conn = init_db(db_path)
            try:
                store_chunks_batch(
                    conn,
                    [{
                        "file": str(indexed_file),
                        "chunk": "print('indexed')",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": indexed_file.stat().st_mtime,
                        "start_line": 1,
                        "end_line": 1,
                        "start_byte": 0,
                        "end_byte": len("print('indexed')\n"),
                        "embedding": [1.0, 0.0],
                    }],
                )
                now = str(time.time())
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS meta "
                    "(key TEXT PRIMARY KEY, value TEXT)"
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?)",
                    ("last_full_index_at", now),
                )
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?)",
                    ("last_refresh_at", now),
                )
                conn.commit()
            finally:
                conn.close()

            filename_result = {
                "path": str(root / "EB1B_Denial_Analysis.pdf"),
                "file": str(root / "EB1B_Denial_Analysis.pdf"),
                "chunk": "size: 1.0 KB    modified: now    type: pdf",
                "snippet": "size: 1.0 KB    modified: now    type: pdf",
                "language": "pdf",
                "start_line": None,
                "end_line": None,
                "score": 1.0,
                "fallback": "filename-lookup",
            }
            decision = cli_module.RouterDecision(
                intent="filename",
                primary_token="eb1b",
                skip_cascade=True,
                skip_filename=False,
                skip_lexical=True,
                confidence=0.95,
                source="llm",
                reason="user asks for a specific file by name",
                out_of_scope="none",
            )

            with patch.dict(
                os.environ,
                {"SKYGREP_DB_PATH": str(db_path), "SKYGREP_NO_HINTS": "1"},
                clear=False,
            ), patch.object(
                cli_module.bootstrap, "preheat_models", return_value=None
            ), patch.object(
                cli_module.bootstrap, "try_autostart_ollama", return_value=False
            ), patch.object(
                cli_module, "route_query", return_value=decision
            ), patch.object(
                cli_module.cfg_mod, "project_root", return_value=root
            ), patch.object(
                cli_module.auto_index,
                "filename_shortcut",
                return_value=[filename_result],
            ), patch.object(
                cli_module, "_symbols_table_populated", return_value=True
            ), patch.object(
                cli_module.code_graph, "populate_graph_table", return_value=0
            ):
                result = runner.invoke(
                    cli_module.cli,
                    ["search", "where is my eb1b file", "--json"],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(result.exception)
        payload = json.loads(result.output)
        self.assertEqual(payload[0]["path"], filename_result["path"])
        self.assertEqual(payload[0]["language"], "pdf")


if __name__ == "__main__":
    unittest.main()
