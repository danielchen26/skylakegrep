# SPDX-License-Identifier: Apache-2.0
"""Tests for the CLI daemon delegation path."""

from __future__ import annotations

from contextlib import ExitStack
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import skylakegrep
from skylakegrep.src import cli as cli_module
from skylakegrep.src import __version__ as cli_version
from skylakegrep.src import server as server_module
from skylakegrep.src.candidate_recall import run_agent_context_search
from skylakegrep.src.storage import init_db, populate_file_embeddings, store_chunks_batch


class _StaticEmbedder:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0] if "token" in text.lower() else [0.0, 1.0]


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

    def test_strict_daemon_result_exits_two_when_verification_is_inconclusive(self):
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
                        "strict_verification": {"status": "inconclusive"},
                    }
                ],
                "latency_seconds": 0.01,
            }
            runner = CliRunner()
            with patch.object(cli_module.cfg_mod, "project_root", return_value=root):
                with patch.object(cli_module, "resolve_scope_facet", return_value=None):
                    with patch(
                        "skylakegrep.src.server.daemon_search",
                        return_value=payload,
                    ) as daemon:
                        result = runner.invoke(
                            cli_module.cli,
                            [
                                "search",
                                "--strict",
                                "--agent-daemon",
                                "where is session refresh implemented?",
                            ],
                        )

            self.assertEqual(result.exit_code, 2, result.output)
            parsed = json.loads(result.output)
            self.assertEqual(
                parsed[0]["strict_verification"]["status"],
                "inconclusive",
            )
            self.assertEqual(daemon.call_args.kwargs["agent_mode"], "context")
            self.assertTrue(daemon.call_args.kwargs["strict"])

    def test_strict_daemon_result_succeeds_when_verification_passes(self):
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
                        "strict_verification": {"status": "passed"},
                    }
                ],
                "latency_seconds": 0.01,
            }
            runner = CliRunner()
            with patch.object(cli_module.cfg_mod, "project_root", return_value=root):
                with patch.object(cli_module, "resolve_scope_facet", return_value=None):
                    with patch(
                        "skylakegrep.src.server.daemon_search",
                        return_value=payload,
                    ):
                        result = runner.invoke(
                            cli_module.cli,
                            [
                                "search",
                                "--strict",
                                "--agent-daemon",
                                "where is session refresh implemented?",
                            ],
                        )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(
                json.loads(result.output)[0]["strict_verification"]["status"],
                "passed",
            )

    def test_daemon_agent_context_matches_shared_direct_contract(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            target = root / "src" / "token_refresh.py"
            target.parent.mkdir(parents=True)
            target.write_text(
                "def refresh_access_token():\n    return 'fresh token'\n",
                encoding="utf-8",
            )
            db_path = root / "index.db"
            conn = init_db(db_path)
            store_chunks_batch(
                conn,
                [
                    {
                        "file": str(target),
                        "chunk": target.read_text(encoding="utf-8"),
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": target.stat().st_mtime,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": target.stat().st_size,
                        "embedding": [1.0, 0.0],
                    }
                ],
            )
            populate_file_embeddings(conn)
            direct, _ = run_agent_context_search(
                conn,
                "where is access token refresh implemented?",
                root,
                top_k=4,
                strict=True,
                embedder_factory=lambda: _StaticEmbedder(),
            )
            conn.close()

            httpd = server_module.ThreadingHTTPServer(
                ("127.0.0.1", 0),
                server_module._SearchHandler,
            )
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            with patch.object(
                server_module,
                "get_config",
                return_value={"db_path": db_path, "rerank_pool": 50},
            ):
                with patch.object(server_module, "resolve_project_root", return_value=root):
                    with patch.object(
                        server_module,
                        "get_embedder",
                        return_value=_StaticEmbedder(),
                    ):
                        thread.start()
                        daemon = server_module.daemon_search(
                            f"http://127.0.0.1:{port}",
                            "where is access token refresh implemented?",
                            top_k=4,
                            agent_mode="context",
                            strict=True,
                            project_root=str(root),
                            lexical_root=str(root),
                            db_path=str(db_path),
                            recall_query="where is access token refresh implemented?",
                        )
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)

        daemon_results = daemon["results"]
        self.assertEqual(
            [item["path"] for item in daemon_results],
            [item["path"] for item in direct],
        )
        self.assertEqual(
            daemon_results[0]["candidate_recall_lanes"],
            direct[0]["candidate_recall_lanes"],
        )
        self.assertEqual(
            daemon_results[0]["agent_summary"],
            direct[0]["agent_summary"],
        )
        self.assertEqual(
            daemon_results[0]["strict_verification"]["status"],
            "passed",
        )

    def test_daemon_rejects_cross_project_index_requests(self):
        import requests

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            db_path = root / "index.db"
            init_db(db_path).close()
            httpd = server_module.ThreadingHTTPServer(
                ("127.0.0.1", 0),
                server_module._SearchHandler,
            )
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            try:
                with patch.object(
                    server_module,
                    "get_config",
                    return_value={"db_path": db_path, "rerank_pool": 50},
                ):
                    with patch.object(
                        server_module,
                        "resolve_project_root",
                        return_value=root,
                    ):
                        thread.start()
                        with self.assertRaises(requests.HTTPError) as raised:
                            server_module.daemon_search(
                                f"http://127.0.0.1:{port}",
                                "where is session refresh implemented?",
                                agent_mode="context",
                                project_root=str(root / "other-project"),
                                lexical_root=str(root / "other-project"),
                                db_path=str(root / "other.db"),
                            )
                self.assertEqual(raised.exception.response.status_code, 409)
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

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
            self.assertEqual(seen_limits, [])
            spawn.assert_not_called()
            self.assertEqual(json.loads(result.output), [])

    def test_serve_becomes_ready_without_loading_optional_reranker(self):
        events: list[str] = []

        class FakeServer:
            def __init__(self, address, handler):
                events.append("bound")

            def serve_forever(self):
                events.append("serving")
                raise KeyboardInterrupt

            def server_close(self):
                events.append("closed")

        with tempfile.TemporaryDirectory() as d:
            cfg = {"db_path": Path(d) / "index.db"}
            with patch.object(server_module, "get_config", return_value=cfg):
                with patch.object(server_module, "ThreadingHTTPServer", FakeServer):
                    with patch.object(server_module, "_start_reranker_warmup") as warm:
                        server_module.serve(host="127.0.0.1", port=7878)

        warm.assert_not_called()
        self.assertEqual(events, ["bound", "serving", "closed"])

    def test_explicit_reranker_warmup_starts_only_after_bind(self):
        events: list[str] = []

        class FakeServer:
            def __init__(self, address, handler):
                events.append("bound")

            def serve_forever(self):
                events.append("serving")
                raise KeyboardInterrupt

            def server_close(self):
                events.append("closed")

        with tempfile.TemporaryDirectory() as d:
            cfg = {"db_path": Path(d) / "index.db"}
            with patch.object(server_module, "get_config", return_value=cfg):
                with patch.object(server_module, "ThreadingHTTPServer", FakeServer):
                    with patch.object(
                        server_module,
                        "_start_reranker_warmup",
                        side_effect=lambda: events.append("warm"),
                    ) as warm:
                        server_module.serve(
                            host="127.0.0.1",
                            port=7878,
                            warm_reranker=True,
                        )

        warm.assert_called_once_with()
        self.assertEqual(events, ["bound", "warm", "serving", "closed"])

    def test_serve_cli_forwards_warm_reranker_option(self):
        runner = CliRunner()
        with patch.object(server_module, "serve") as serve:
            result = runner.invoke(
                cli_module.cli,
                ["serve", "--port", "7979", "--warm-reranker"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        serve.assert_called_once_with(
            host="127.0.0.1",
            port=7979,
            warm_reranker=True,
        )


if __name__ == "__main__":
    unittest.main()
