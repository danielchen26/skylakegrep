"""Tests for the 0.4.0 UX changes: bare-form routing, doctor, default flips."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from local_mgrep.src import cli as cli_module
from local_mgrep.src import config as config_module


class BareFormRoutingTests(unittest.TestCase):
    """``mgrep "<query>"`` should route to ``search`` automatically."""

    def test_unknown_first_arg_routes_to_search(self):
        # Verified by parsing args through MgrepCLI.parse_args directly: any
        # non-flag, non-subcommand first token gets prepended with ``search``.
        ctx = cli_module.cli.make_context(
            "mgrep", [], resilient_parsing=True
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
        ctx = cli_module.cli.make_context("mgrep", [], resilient_parsing=True)
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
        with patch("local_mgrep.src.bootstrap._probe_ollama", return_value=(False, "connection refused")):
            result = runner.invoke(cli_module.cli, ["doctor"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("ollama is required", result.output.lower())
        self.assertIn("connection refused", result.output.lower())

    def test_doctor_reports_present_models(self):
        runner = CliRunner()
        with patch("local_mgrep.src.bootstrap._probe_ollama", return_value=(True, "")):
            with patch(
                "local_mgrep.src.bootstrap.list_local_models",
                return_value=["nomic-embed-text:latest", "qwen2.5:3b"],
            ):
                result = runner.invoke(cli_module.cli, ["doctor"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("nomic-embed-text", result.output)


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
            with patch.dict(os.environ, {"MGREP_DB_PATH": str(override)}, clear=False):
                self.assertEqual(config_module.resolve_db_path(), override)

    def test_resolve_db_path_uses_project_scoped_default(self):
        env = dict(os.environ)
        env.pop("MGREP_DB_PATH", None)
        with patch.dict(os.environ, env, clear=True):
            path = config_module.resolve_db_path()
            self.assertIn(".local-mgrep/repos", str(path))


if __name__ == "__main__":
    unittest.main()
