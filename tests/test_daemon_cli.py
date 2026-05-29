"""Tests for the CLI daemon delegation path."""

from __future__ import annotations

from contextlib import ExitStack
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

    def test_agent_fast_uses_daemon_path_anchor_preset(self):
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
                    with patch("skylakegrep.src.server.daemon_search", return_value=payload) as daemon:
                        result = runner.invoke(
                            cli_module.cli,
                            [
                                "search",
                                "--agent-fast",
                                "--agent-daemon",
                                "where is session refresh implemented?",
                            ],
                        )

            self.assertEqual(result.exit_code, 0, result.output)
            parsed = json.loads(result.output)
            self.assertNotIn("snippet", parsed[0])
            kwargs = daemon.call_args.kwargs
            self.assertEqual(kwargs["top_k"], 10)
            self.assertFalse(kwargs["rerank"])

    def test_agent_context_uses_daemon_snippet_preset(self):
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
                    with patch("skylakegrep.src.server.daemon_search", return_value=payload) as daemon:
                        result = runner.invoke(
                            cli_module.cli,
                            [
                                "search",
                                "--agent-context",
                                "--agent-daemon",
                                "what does session refresh do?",
                            ],
                        )

            self.assertEqual(result.exit_code, 0, result.output)
            parsed = json.loads(result.output)
            self.assertEqual(parsed[0]["snippet"], "def refresh_session(): pass")
            self.assertEqual(daemon.call_args.kwargs["top_k"], 8)
            self.assertFalse(daemon.call_args.kwargs["rerank"])

    def test_agent_context_defers_foreground_refresh_work(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db_path = root / "index.db"
            seen_limits: list[int | None] = []
            decision = cli_module.RouterDecision(
                intent="semantic",
                skip_filename=True,
                skip_lexical=False,
                confidence=0.93,
                source="fast-intent",
                reason="semantic query",
                out_of_scope="none",
            )

            def _refresh(*args, **kwargs):
                seen_limits.append(kwargs.get("max_foreground_files"))
                return -3

            runner = CliRunner()
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(cli_module.cfg_mod, "project_root", return_value=root)
                )
                stack.enter_context(
                    patch.object(cli_module, "resolve_scope_facet", return_value=None)
                )
                stack.enter_context(
                    patch.object(
                        cli_module,
                        "get_config",
                        return_value={"db_path": db_path, "rerank_pool": 50},
                    )
                )
                stack.enter_context(
                    patch.object(cli_module.bootstrap, "preheat_models", return_value=None)
                )
                stack.enter_context(
                    patch.object(
                        cli_module.bootstrap,
                        "try_autostart_ollama",
                        return_value=False,
                    )
                )
                stack.enter_context(
                    patch.object(cli_module, "route_query", return_value=decision)
                )
                stack.enter_context(
                    patch.object(cli_module.auto_index, "is_index_ready", return_value=True)
                )
                stack.enter_context(
                    patch.object(
                        cli_module.auto_index,
                        "incremental_refresh",
                        side_effect=_refresh,
                    )
                )
                spawn = stack.enter_context(
                    patch.object(
                        cli_module.auto_index,
                        "spawn_background_index",
                        return_value=123,
                    )
                )
                stack.enter_context(
                    patch.object(
                        cli_module.auto_index,
                        "index_status",
                        return_value={"chunks": 0, "files": 0},
                    )
                )
                result = runner.invoke(
                    cli_module.cli,
                    [
                        "search",
                        "--agent-context",
                        "what does session refresh do?",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(seen_limits, [0])
            spawn.assert_called_once()
            self.assertEqual(json.loads(result.output), [])


if __name__ == "__main__":
    unittest.main()
