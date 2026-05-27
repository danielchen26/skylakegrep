"""Tests for the CLI daemon delegation path."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import skylakegrep
from skylakegrep.src import cli as cli_module
from skylakegrep.src import __version__ as cli_version


class DaemonCliTests(unittest.TestCase):
    def test_package_and_cli_versions_match(self):
        self.assertEqual(skylakegrep.__version__, cli_version)

    def test_daemon_json_path_has_project_scope_before_rendering(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            payload = {
                "results": [
                    {
                        "path": "src/session.py",
                        "start_line": 1,
                        "end_line": 3,
                        "language": "python",
                        "score": 0.9,
                        "snippet": "def refresh_session(): pass",
                    }
                ],
                "latency_seconds": 0.01,
            }
            runner = CliRunner()
            with patch.object(cli_module.cfg_mod, "project_root", return_value=root):
                with patch.object(cli_module, "resolve_scope_facet", return_value=None):
                    with patch("skylakegrep.src.server.daemon_search", return_value=payload):
                        result = runner.invoke(
                            cli_module.cli,
                            [
                                "search",
                                "--daemon-url",
                                "http://127.0.0.1:7879",
                                "--json",
                                "where is session refresh implemented?",
                            ],
                        )

            self.assertEqual(result.exit_code, 0, result.output)
            parsed = json.loads(result.output)
            self.assertEqual(parsed[0]["path"], "src/session.py")

    def test_daemon_json_no_content_omits_snippet(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            payload = {
                "results": [
                    {
                        "path": "src/session.py",
                        "start_line": 1,
                        "end_line": 3,
                        "language": "python",
                        "score": 0.9,
                        "snippet": "def refresh_session(): pass",
                    }
                ],
                "latency_seconds": 0.01,
            }
            runner = CliRunner()
            with patch.object(cli_module.cfg_mod, "project_root", return_value=root):
                with patch.object(cli_module, "resolve_scope_facet", return_value=None):
                    with patch("skylakegrep.src.server.daemon_search", return_value=payload):
                        result = runner.invoke(
                            cli_module.cli,
                            [
                                "search",
                                "--daemon-url",
                                "http://127.0.0.1:7879",
                                "--json",
                                "--no-content",
                                "where is session refresh implemented?",
                            ],
                        )

            self.assertEqual(result.exit_code, 0, result.output)
            parsed = json.loads(result.output)
            self.assertNotIn("snippet", parsed[0])


if __name__ == "__main__":
    unittest.main()
