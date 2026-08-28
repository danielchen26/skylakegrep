# SPDX-License-Identifier: Apache-2.0
"""Tests for the 0.4.0 UX changes: bare-form routing, doctor, default flips."""

from __future__ import annotations

import json
import os
import re
import sqlite3
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

    def test_main_propagates_click_exit_code(self):
        with patch.object(cli_module, "cli", return_value=7):
            self.assertEqual(cli_module.main(), 7)

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

    def test_search_command_name_is_explicit(self):
        self.assertIn("search", cli_module.cli.commands)
        self.assertNotIn("search-cmd", cli_module.cli.commands)

    def test_bare_detail_flag_is_full_detail_shorthand(self):
        self.assertEqual(
            cli_module._normalize_search_cli_args(["--detail", "where is config"]),
            ["--detail=full", "where is config"],
        )
        self.assertEqual(
            cli_module._normalize_search_cli_args(["--detail", "summary", "where is config"]),
            ["--detail", "summary", "where is config"],
        )

    def test_include_is_hard_boundary_for_absolute_output_paths(self):
        root = Path("/repo")
        rows = [
            {"path": "/repo/skylakegrep/src/integrations.py"},
            {"path": "/Users/example/Downloads/private.json"},
        ]
        filtered = cli_module._apply_result_boundaries(
            rows,
            project_root=root,
            explicit_scope=False,
            include_patterns=("skylakegrep/src/integrations.py",),
            exclude_patterns=(),
        )
        self.assertEqual(filtered, [rows[0]])

    def test_exclude_is_hard_boundary_for_absolute_output_paths(self):
        root = Path("/repo")
        rows = [
            {"path": "/repo/skylakegrep/src/render.py"},
            {"path": "/repo/tests/test_terminal_ui.py"},
        ]
        filtered = cli_module._apply_result_boundaries(
            rows,
            project_root=root,
            explicit_scope=False,
            include_patterns=(),
            exclude_patterns=("tests/**",),
        )
        self.assertEqual(filtered, [rows[0]])

    def test_help_flag_does_not_route_to_search(self):
        runner = CliRunner()
        result = runner.invoke(cli_module.cli, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Common usage", result.output)
        self.assertIn("Information depth", result.output)
        self.assertIn("--content --detail standard", result.output)
        self.assertIn("--agent-context", result.output)

    def test_search_help_lists_information_depth_examples(self):
        runner = CliRunner()
        result = runner.invoke(cli_module.cli, ["search", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Information depth", result.output)
        self.assertIn("--detail full", result.output)
        self.assertIn("--agent-fast", result.output)
        self.assertIn("--agent-context", result.output)

    def test_version_flag_does_not_route_to_search(self):
        runner = CliRunner()
        result = runner.invoke(cli_module.cli, ["--version"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn(cli_module.__version__, result.output)

    def test_metadata_query_returns_before_ollama_preheat(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            old = root / "old.txt"
            new = root / "new.txt"
            old.write_text("old\n")
            new.write_text("new\n")
            now = time.time()
            os.utime(old, (now - 100, now - 100))
            os.utime(new, (now, now - 50))

            with patch.object(
                cli_module, "get_config", return_value={"db_path": root / "x.db"}
            ), patch.object(
                cli_module.cfg_mod, "project_root", return_value=root
            ), patch.object(
                cli_module.bootstrap,
                "preheat_models",
                side_effect=AssertionError("metadata lane should not preheat"),
            ):
                result = runner.invoke(
                    cli_module.cli,
                    ["search", "what is the latest 2 files i opened"],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("metadata-opened", result.output)
        self.assertIn("new.txt", result.output)
        self.assertIn("old.txt", result.output)

    def test_scoped_metadata_modifier_file_discovery_returns_before_embedding(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            scoped = root / "CASE42"
            scoped.mkdir()
            wanted = scoped / "project_brief.pdf"
            unrelated = scoped / "unrelated_notes.pdf"
            wanted.write_text("brief\n")
            unrelated.write_text("notes\n")
            db_path = root / "index.db"
            decision = cli_module.RouterDecision(
                intent="mixed",
                primary_token="",
                skip_cascade=False,
                skip_filename=False,
                skip_lexical=False,
                confidence=0.85,
                source="fast-intent",
                reason="metadata modifier with target descriptors",
                out_of_scope="none",
                metadata_kind="created",
                metadata_terminal=False,
            )

            with patch.dict(
                os.environ,
                {"SKYGREP_DB_PATH": str(db_path), "SKYGREP_NO_HINTS": "1"},
                clear=False,
            ), patch.object(
                cli_module,
                "get_config",
                return_value={"db_path": db_path, "rerank_pool": 50},
            ), patch.object(
                cli_module.cfg_mod, "project_root", return_value=root
            ), patch.object(
                cli_module.bootstrap, "preheat_models", return_value=None
            ), patch.object(
                cli_module.bootstrap, "try_autostart_ollama", return_value=False
            ), patch.object(
                cli_module, "route_query", return_value=decision
            ), patch.object(
                cli_module.auto_index, "filename_shortcut", return_value=None
            ), patch.object(
                cli_module,
                "get_embedder",
                side_effect=AssertionError("semantic embedding should not run"),
            ):
                result = runner.invoke(
                    cli_module.cli,
                    [
                        "search",
                        "Show me where my project brief that I recently "
                        "created in CASE42 folder",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("scoped-file-discovery", result.output)
        self.assertIn("project_brief.pdf", result.output)
        self.assertNotIn("unrelated_notes.pdf", result.output)

    def test_no_cascade_footer_handles_plain_semantic_pool(self):
        from skylakegrep.src.storage import init_db, store_chunks_batch

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "src" / "policy.py"
            target.parent.mkdir(parents=True)
            target.write_text("def retry_policy(): pass\n")
            db_path = root / "index.db"
            conn = init_db(db_path)
            try:
                store_chunks_batch(
                    conn,
                    [{
                        "file": str(target),
                        "chunk": "def retry_policy(): pass",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": target.stat().st_mtime,
                        "start_line": 1,
                        "end_line": 1,
                        "start_byte": 0,
                        "end_byte": len("def retry_policy(): pass\n"),
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

            decision = cli_module.RouterDecision(
                intent="semantic",
                primary_token="",
                skip_cascade=False,
                skip_filename=True,
                skip_lexical=True,
                confidence=0.9,
                source="fast-intent",
                reason="semantic query",
                out_of_scope="none",
            )
            semantic_result = {
                "path": str(target),
                "file": str(target),
                "chunk": "def retry_policy(): pass",
                "snippet": "def retry_policy(): pass",
                "language": "python",
                "start_line": 1,
                "end_line": 1,
                "score": 0.9,
            }

            class _FakeEmbedder:
                def embed(self, text):
                    return [1.0, 0.0]

            def _slow_refresh(*args, **kwargs):
                time.sleep(0.05)
                return 0

            with patch.dict(
                os.environ,
                {"SKYGREP_DB_PATH": str(db_path), "SKYGREP_NO_HINTS": "1"},
                clear=False,
            ), patch.object(
                cli_module,
                "get_config",
                return_value={"db_path": db_path, "rerank_pool": 50},
            ), patch.object(
                cli_module.cfg_mod, "project_root", return_value=root
            ), patch.object(
                cli_module.bootstrap, "preheat_models", return_value=None
            ), patch.object(
                cli_module.bootstrap, "try_autostart_ollama", return_value=False
            ), patch.object(
                cli_module, "route_query", return_value=decision
            ), patch.object(
                cli_module.auto_index, "incremental_refresh", side_effect=_slow_refresh
            ), patch.object(
                cli_module, "_symbols_table_populated", return_value=True
            ), patch.object(
                cli_module.code_graph, "populate_graph_table", return_value=0
            ), patch.object(
                cli_module, "get_embedder", return_value=_FakeEmbedder()
            ), patch.object(
                cli_module, "maybe_start_recovery", return_value=None
            ), patch.object(
                cli_module, "search", return_value=[semantic_result]
            ):
                result = runner.invoke(
                    cli_module.cli,
                    [
                        "search",
                        "--auto-index",
                        "--no-cascade",
                        "explain retry policy",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("retry_policy", result.output)
        match = re.search(r"\n╰─ done\s+([0-9.]+)s · quality", result.output)
        self.assertIsNotNone(match, result.output)
        self.assertGreaterEqual(float(match.group(1)), 0.04)

    def test_agent_context_skips_filename_and_refresh_slow_lanes(self):
        from skylakegrep.src.storage import init_db, store_chunks_batch

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "src" / "worker_topology.rs"
            target.parent.mkdir(parents=True)
            target.write_text(
                "struct WorkerTopologyAdaptiveHarnessProvider;\n",
                encoding="utf-8",
            )
            db_path = root / "index.db"
            conn = init_db(db_path)
            try:
                store_chunks_batch(
                    conn,
                    [{
                        "file": str(target),
                        "chunk": "struct WorkerTopologyAdaptiveHarnessProvider;",
                        "language": "rust",
                        "chunk_index": 0,
                        "file_mtime": target.stat().st_mtime,
                        "start_line": 1,
                        "end_line": 1,
                        "start_byte": 0,
                        "end_byte": len("struct WorkerTopologyAdaptiveHarnessProvider;\n"),
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

            decision = cli_module.RouterDecision(
                intent="semantic",
                primary_token="WorkerTopologyAdaptiveHarnessProvider",
                skip_cascade=False,
                skip_filename=False,
                skip_lexical=False,
                confidence=0.80,
                source="fast-intent",
                reason="semantic evidence request",
                out_of_scope="none",
            )

            with patch.dict(
                os.environ,
                {"SKYGREP_DB_PATH": str(db_path), "SKYGREP_NO_HINTS": "1"},
                clear=False,
            ), patch.object(
                cli_module,
                "get_config",
                return_value={"db_path": db_path, "rerank_pool": 50},
            ), patch.object(
                cli_module.cfg_mod, "project_root", return_value=root
            ), patch.object(
                cli_module.bootstrap, "preheat_models", return_value=None
            ), patch.object(
                cli_module.bootstrap, "try_autostart_ollama", return_value=False
            ), patch.object(
                cli_module, "route_query", return_value=decision
            ), patch.object(
                cli_module.auto_index,
                "incremental_refresh",
                side_effect=AssertionError("agent-context should skip refresh scan"),
            ), patch.object(
                cli_module.auto_index,
                "filename_shortcut",
                side_effect=AssertionError("agent-context should skip filename scan"),
            ):
                result = runner.invoke(
                    cli_module.cli,
                    [
                        "search",
                        "--auto-index",
                        "--agent-context",
                        "--include",
                        "src/**",
                        "WorkerTopology Adaptive Harness provider",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload[0]["path"], str(target))

    def test_search_accepts_query_split_by_smart_quotes(self):
        runner = CliRunner()
        with patch.object(
            cli_module.bootstrap, "preheat_models", return_value=None
        ), patch.object(
            cli_module, "get_config", return_value={"db_path": Path("/tmp/noop.db")}
        ), patch.object(
            cli_module, "route_query",
            side_effect=RuntimeError("stop after normalized query"),
        ) as routed:
            result = runner.invoke(
                cli_module.cli,
                ["search", "“where", "is", "my", "case42", "file", "in", "Downloads”"],
            )
        self.assertIsInstance(result.exception, RuntimeError)
        routed.assert_called_once()
        self.assertEqual(
            routed.call_args.args[0],
            "where is my case42 file in Downloads",
        )

    def test_bare_form_accepts_query_split_by_smart_quotes(self):
        runner = CliRunner()
        with patch.object(
            cli_module.bootstrap, "preheat_models", return_value=None
        ), patch.object(
            cli_module, "get_config", return_value={"db_path": Path("/tmp/noop.db")}
        ), patch.object(
            cli_module, "route_query",
            side_effect=RuntimeError("stop after normalized query"),
        ) as routed:
            result = runner.invoke(
                cli_module.cli,
                ["“where", "is", "my", "case42", "file", "in", "Downloads”"],
            )
        self.assertIsInstance(result.exception, RuntimeError)
        routed.assert_called_once()
        self.assertEqual(
            routed.call_args.args[0],
            "where is my case42 file in Downloads",
        )


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
                with patch(
                    "skylakegrep.src.cli.importlib.util.find_spec",
                    return_value=object(),
                ) as find_spec:
                    result = runner.invoke(cli_module.cli, ["doctor"])
        self.assertEqual(result.exit_code, 0, result.output)
        find_spec.assert_called_once_with("sentence_transformers")
        self.assertIn("sentence-transformers installed", result.output)
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
    def test_cold_merge_upgrades_filename_anchor_to_lazy_content(self):
        path = "/tmp/CASE42_Project_Report.txt"
        filename = {
            "path": path,
            "snippet": "size: 0.1 KB    modified: ?    type: txt",
            "chunk": "size: 0.1 KB    modified: ?    type: txt",
            "language": "txt",
            "score": 1.0,
            "fallback": "filename-lookup",
        }
        lazy = {
            "path": path,
            "snippet": "retry policy uses exponential backoff",
            "chunk": "retry policy uses exponential backoff",
            "language": "txt",
            "score": 0.42,
        }

        merged = cli_module._merge_sources_preferring_depth(
            ([filename], [lazy], [], []),
            top=5,
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["snippet"], lazy["snippet"])

    def test_semantic_filename_anchor_scopes_warm_results(self):
        path = "/tmp/CASE42_Project_Report.txt"
        filename = {
            "path": path,
            "snippet": "size: 0.1 KB    modified: ?    type: txt",
            "chunk": "size: 0.1 KB    modified: ?    type: txt",
            "language": "txt",
            "score": 1.0,
            "fallback": "filename-lookup",
        }
        same_file_depth = {
            "path": path,
            "snippet": "retry policy uses exponential backoff",
            "chunk": "retry policy uses exponential backoff",
            "language": "txt",
            "score": 0.42,
        }
        unrelated_semantic = {
            "path": "/tmp/unrelated.py",
            "snippet": "retry token appears in unrelated code",
            "chunk": "retry token appears in unrelated code",
            "language": "python",
            "score": 0.99,
        }
        decision = cli_module.RouterDecision(
            intent="semantic",
            primary_token="CASE42_Project_Report",
            skip_cascade=False,
            skip_filename=False,
            skip_lexical=False,
            confidence=0.9,
            source="fast-intent",
            reason="semantic query with a concrete filename anchor",
            out_of_scope="none",
        )

        self.assertTrue(
            cli_module._semantic_filename_anchor_should_lead(
                decision, [filename]
            )
        )
        merged = cli_module._merge_sources_preferring_depth(
            ([filename], [unrelated_semantic, same_file_depth], []),
            top=2,
        )

        self.assertEqual(merged[0]["path"], path)
        self.assertEqual(merged[0]["snippet"], same_file_depth["snippet"])
        self.assertEqual(merged[1]["path"], unrelated_semantic["path"])

    def test_semantic_depth_query_vetoes_filename_finality(self):
        decision = cli_module.RouterDecision(
            intent="filename",
            primary_token="CASE42",
            skip_cascade=True,
            skip_filename=False,
            skip_lexical=False,
            confidence=0.95,
            source="llm",
            reason="misclassified as filename",
            out_of_scope="none",
        )

        self.assertFalse(
            cli_module._filename_evidence_satisfies_depth(
                "what does CASE42 file say about retries",
                decision,
                detail="standard",
                answer=False,
                agentic=False,
            )
        )
        self.assertTrue(
            cli_module._filename_evidence_satisfies_depth(
                "where is CASE42 file",
                decision,
                detail="standard",
                answer=False,
                agentic=False,
            )
        )

    def test_cold_filename_proactive_runs_before_lazy_semantic(self):
        """Wrong-folder filename queries should not wait for lazy embedding."""

        from skylakegrep.src import proactive as proactive_module

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            db_path = Path(temp_dir) / "index.db"
            outside = Path(temp_dir) / "Downloads" / "CASE42_Project_Report.pdf"
            outside.parent.mkdir()
            outside.write_text("placeholder\n")
            decision = cli_module.RouterDecision(
                intent="filename",
                primary_token="CASE42",
                skip_cascade=False,
                skip_filename=False,
                skip_lexical=False,
                confidence=0.95,
                source="fast-intent",
                reason="filename lookup",
                out_of_scope="none",
            )
            proactive_result = proactive_module.ProactiveResult(
                enhancer_name="filename_extend",
                extra_hits=[
                    {
                        "path": str(outside),
                        "score": 0.0,
                        "language": "pdf",
                        "search_dir": str(outside.parent),
                        "source": "proactive:filename_extend",
                    }
                ],
                note="Found 1 match outside cwd:",
                commands=[],
            )

            with patch.dict(
                os.environ,
                {"SKYGREP_DB_PATH": str(db_path)},
                clear=False,
            ), patch.object(
                cli_module, "route_query", return_value=decision
            ), patch.object(
                cli_module.cfg_mod, "project_root", return_value=root
            ), patch.object(
                cli_module.bootstrap, "preheat_models", return_value=None
            ), patch.object(
                cli_module.bootstrap, "try_autostart_ollama", return_value=False
            ), patch.object(
                cli_module.auto_index, "is_index_ready", return_value=False
            ), patch.object(
                cli_module.auto_index, "spawn_background_index", return_value=None
            ), patch.object(
                cli_module.auto_index, "filename_shortcut", return_value=None
            ), patch.object(
                cli_module.auto_index, "rg_fallback_results", return_value=[]
            ) as rg_mock, patch.object(
                proactive_module,
                "run_enhancers_parallel",
                return_value=([proactive_result], {}),
            ), patch.object(
                cli_module,
                "get_embedder",
                side_effect=AssertionError("lazy semantic should not run"),
            ):
                result = runner.invoke(
                    cli_module.cli,
                    ["search", "where is CASE42 file", "--auto-index"],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        rg_mock.assert_not_called()
        self.assertIn("├─ proactive  no local filename hit yet", result.output)
        self.assertIn("searching configured roots", result.output)
        self.assertIn("proactive-filename", result.output)
        self.assertIn("lazy-skipped", result.output)
        self.assertIn("CASE42_Project_Report.pdf", result.output)

    def test_cold_filename_proactive_full_detail_extracts_content(self):
        """Full-detail filename hits should show body text without lazy
        semantic indexing."""

        from skylakegrep.src import proactive as proactive_module

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            db_path = Path(temp_dir) / "index.db"
            outside = Path(temp_dir) / "Downloads" / "CASE42_Project_Report.txt"
            outside.parent.mkdir()
            outside.write_text("generic report body marker\n", encoding="utf-8")
            decision = cli_module.RouterDecision(
                intent="filename",
                primary_token="CASE42",
                skip_cascade=False,
                skip_filename=False,
                skip_lexical=False,
                confidence=0.95,
                source="fast-intent",
                reason="filename lookup",
                out_of_scope="none",
            )
            proactive_result = proactive_module.ProactiveResult(
                enhancer_name="filename_extend",
                extra_hits=[
                    {
                        "path": str(outside),
                        "file": str(outside),
                        "chunk": "size: 0.0 KB    modified: ?    type: txt",
                        "snippet": "size: 0.0 KB    modified: ?    type: txt",
                        "language": "txt",
                        "start_line": None,
                        "end_line": None,
                        "score": 1.0,
                        "fallback": "filename-lookup",
                        "filename_token": "CASE42",
                        "search_dir": str(outside.parent),
                        "source": "proactive:filename_extend",
                    }
                ],
                note="Found 1 match outside cwd:",
                commands=[],
            )

            with patch.dict(
                os.environ,
                {"SKYGREP_DB_PATH": str(db_path)},
                clear=False,
            ), patch.object(
                cli_module, "route_query", return_value=decision
            ), patch.object(
                cli_module.cfg_mod, "project_root", return_value=root
            ), patch.object(
                cli_module.bootstrap, "preheat_models", return_value=None
            ), patch.object(
                cli_module.bootstrap, "try_autostart_ollama", return_value=False
            ), patch.object(
                cli_module.auto_index, "is_index_ready", return_value=False
            ), patch.object(
                cli_module.auto_index, "spawn_background_index", return_value=None
            ), patch.object(
                cli_module.auto_index, "filename_shortcut", return_value=None
            ), patch.object(
                cli_module.auto_index, "rg_fallback_results", return_value=[]
            ), patch.object(
                proactive_module,
                "run_enhancers_parallel",
                return_value=([proactive_result], {}),
            ), patch.object(
                cli_module,
                "get_embedder",
                side_effect=AssertionError("lazy semantic should not run"),
            ):
                result = runner.invoke(
                    cli_module.cli,
                    [
                        "search",
                        "where is CASE42 file",
                        "--auto-index",
                        "--detail",
                        "full",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("proactive-filename", result.output)
        self.assertIn("lazy-skipped", result.output)
        self.assertIn("generic report body marker", result.output)

    def test_cold_semantic_cross_folder_timeout_returns_partial_answer(self):
        from skylakegrep.src import lazy_indexer as lazy_module

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            target = root / "src" / "session_memory.ts"
            target.parent.mkdir()
            target.write_text("export function renewSession() {}\n", encoding="utf-8")
            db_path = Path(temp_dir) / "index.db"
            decision = cli_module.RouterDecision(
                intent="semantic",
                primary_token="session refresh logic",
                skip_cascade=False,
                skip_filename=True,
                skip_lexical=False,
                confidence=0.93,
                source="fast-intent",
                reason="semantic query",
                out_of_scope="none",
            )
            lazy_result = {
                "path": str(target),
                "file": str(target),
                "chunk": "export function renewSession() {}",
                "snippet": "export function renewSession() {}",
                "language": "typescript",
                "start_line": 1,
                "end_line": 1,
                "score": 0.8,
            }

            def _slow_cross(*args, **kwargs):
                time.sleep(2.0)
                return [], {"path": "lazy-cross-folder", "late": True}

            with patch.dict(
                os.environ,
                {
                    "SKYGREP_DB_PATH": str(db_path),
                    "SKYGREP_NO_HINTS": "1",
                    "SKYGREP_COLD_LAZY_TOTAL_BUDGET_S": "1",
                    "SKYGREP_COLD_LAZY_CWD_BUDGET_S": "1",
                    "SKYGREP_COLD_LAZY_CROSS_BUDGET_S": "1",
                },
                clear=False,
            ), patch.object(
                cli_module, "get_config", return_value={"db_path": db_path}
            ), patch.object(
                cli_module.cfg_mod, "project_root", return_value=root
            ), patch.object(
                cli_module.bootstrap, "preheat_models", return_value=None
            ), patch.object(
                cli_module.bootstrap, "try_autostart_ollama", return_value=False
            ), patch.object(
                cli_module, "route_query", return_value=decision
            ), patch.object(
                cli_module.auto_index, "is_index_ready", return_value=False
            ), patch.object(
                cli_module.auto_index, "spawn_background_index", return_value=None
            ), patch.object(
                cli_module.auto_index, "filename_shortcut", return_value=None
            ), patch.object(
                cli_module.auto_index, "rg_fallback_results", return_value=[]
            ), patch.object(
                cli_module, "get_embedder", return_value=object()
            ), patch.object(
                lazy_module,
                "lazy_explore_cold_start",
                return_value=(
                    [lazy_result],
                    {
                        "sigma": 0.05,
                        "confidence": "medium",
                        "embed_new": 1,
                        "embed_cached": 0,
                    },
                ),
            ), patch.object(
                lazy_module, "lazy_explore_cross_folder", side_effect=_slow_cross
            ):
                start = time.perf_counter()
                result = runner.invoke(
                    cli_module.cli,
                    ["search", "where does session refresh logic live?", "--auto-index"],
                )
                elapsed = time.perf_counter() - start

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertLess(elapsed, 1.8, result.output)
        self.assertIn("session_memory.ts", result.output)
        self.assertIn("cross-folder timed out", result.output)
        self.assertIn("quality=BEST", result.output)
        self.assertIn("path", result.output)
        self.assertIn("router", result.output)
        self.assertIn("pool", result.output)
        self.assertIn("budget", result.output)
        self.assertNotIn("🌊", result.output)
        self.assertNotIn("💧", result.output)
        self.assertNotIn("⚡", result.output)
        self.assertNotIn("▾", result.output)
        self.assertIn("╰─ done", result.output)

    def test_cold_semantic_cross_folder_db_lock_is_status_not_failure(self):
        from skylakegrep.src import lazy_indexer as lazy_module

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            db_path = Path(temp_dir) / "index.db"
            decision = cli_module.RouterDecision(
                intent="semantic",
                primary_token="rate limiter redesign",
                skip_cascade=False,
                skip_filename=True,
                skip_lexical=False,
                confidence=0.70,
                source="llm",
                reason="semantic query",
                out_of_scope="none",
            )

            with patch.dict(
                os.environ,
                {
                    "SKYGREP_DB_PATH": str(db_path),
                    "SKYGREP_NO_HINTS": "1",
                    "SKYGREP_COLD_LAZY_TOTAL_BUDGET_S": "1",
                    "SKYGREP_COLD_LAZY_CWD_BUDGET_S": "1",
                    "SKYGREP_COLD_LAZY_CROSS_BUDGET_S": "1",
                },
                clear=False,
            ), patch.object(
                cli_module, "get_config", return_value={"db_path": db_path}
            ), patch.object(
                cli_module.cfg_mod, "project_root", return_value=root
            ), patch.object(
                cli_module.bootstrap, "preheat_models", return_value=None
            ), patch.object(
                cli_module.bootstrap, "try_autostart_ollama", return_value=False
            ), patch.object(
                cli_module, "route_query", return_value=decision
            ), patch.object(
                cli_module.auto_index, "is_index_ready", return_value=False
            ), patch.object(
                cli_module.auto_index, "spawn_background_index", return_value=None
            ), patch.object(
                cli_module.auto_index, "filename_shortcut", return_value=None
            ), patch.object(
                cli_module.auto_index, "rg_fallback_results", return_value=[]
            ), patch.object(
                cli_module, "get_embedder", return_value=object()
            ), patch.object(
                lazy_module, "lazy_explore_cold_start", return_value=([], {})
            ), patch.object(
                lazy_module,
                "lazy_explore_cross_folder",
                side_effect=sqlite3.OperationalError("database is locked"),
            ):
                result = runner.invoke(
                    cli_module.cli,
                    ["search", "the design doc on rate limiter rewrite", "--auto-index"],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("background index is writing", result.output)
        self.assertIn("No matches yet", result.output)
        self.assertNotIn("lazy cross-folder failed", result.output)

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
                "path": str(root / "CASE42_Project_Report.pdf"),
                "file": str(root / "CASE42_Project_Report.pdf"),
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
                primary_token="case42",
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
                    ["search", "where is my case42 file", "--json"],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(result.exception)
        payload = json.loads(result.output)
        self.assertEqual(payload[0]["path"], filename_result["path"])
        self.assertEqual(payload[0]["language"], "pdf")

    def test_filename_skip_request_without_evidence_still_runs_cascade(self):
        """A filename-like route is not enough to suppress semantic recall."""

        from skylakegrep.src.storage import init_db, store_chunks_batch

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            indexed_file = root / "src" / "semantic_answer.py"
            indexed_file.parent.mkdir(parents=True)
            indexed_file.write_text("def semantic_answer(): pass\n")
            db_path = Path(temp_dir) / "index.db"
            conn = init_db(db_path)
            try:
                store_chunks_batch(
                    conn,
                    [{
                        "file": str(indexed_file),
                        "chunk": "def semantic_answer(): pass",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": indexed_file.stat().st_mtime,
                        "start_line": 1,
                        "end_line": 1,
                        "start_byte": 0,
                        "end_byte": len("def semantic_answer(): pass\n"),
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

            decision = cli_module.RouterDecision(
                intent="filename",
                primary_token="case42",
                skip_cascade=True,
                skip_filename=False,
                skip_lexical=True,
                confidence=0.95,
                source="llm",
                reason="user asks for a specific file by name",
                out_of_scope="none",
            )
            semantic_result = {
                "path": str(indexed_file),
                "file": str(indexed_file),
                "chunk": "def semantic_answer(): pass",
                "snippet": "def semantic_answer(): pass",
                "language": "python",
                "start_line": 1,
                "end_line": 1,
                "score": 0.8,
            }

            class _FakeEmbedder:
                def embed(self, text):
                    return [1.0, 0.0]

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
                cli_module.auto_index, "incremental_refresh", return_value=0
            ), patch.object(
                cli_module.auto_index, "filename_shortcut", return_value=None
            ), patch.object(
                cli_module.auto_index, "lexical_shortcut", return_value=None
            ), patch.object(
                cli_module, "_symbols_table_populated", return_value=True
            ), patch.object(
                cli_module.code_graph, "populate_graph_table", return_value=0
            ), patch.object(
                cli_module, "get_embedder", return_value=_FakeEmbedder()
            ) as get_embedder_mock, patch.object(
                cli_module, "get_answerer", return_value=object()
            ), patch.object(
                cli_module, "maybe_start_recovery", return_value=None
            ), patch.object(
                cli_module,
                "cascade_search",
                return_value=(
                    [semantic_result],
                    {
                        "path": "cosine-cheap",
                        "early_exit": True,
                        "gap": 1.0,
                        "tau": 0.1,
                    },
                ),
            ):
                result = runner.invoke(
                    cli_module.cli,
                    ["search", "where is my case42 file", "--json"],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(get_embedder_mock.called, "cascade should still run")
        payload = json.loads(result.output)
        self.assertEqual(payload[0]["path"], str(indexed_file))

    def test_rg_shortcut_does_not_skip_cascade(self):
        """rg shortcut may preview/rank, but semantic cascade still runs."""

        from skylakegrep.src.storage import init_db, store_chunks_batch

        runner = CliRunner()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "project"
            root.mkdir()
            indexed_file = root / "src" / "search" / "filename_shortcut.py"
            indexed_file.parent.mkdir(parents=True)
            indexed_file.write_text("def filename_shortcut(): pass\n")
            db_path = Path(temp_dir) / "index.db"
            conn = init_db(db_path)
            try:
                store_chunks_batch(
                    conn,
                    [{
                        "file": str(indexed_file),
                        "chunk": "def filename_shortcut(): pass",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": indexed_file.stat().st_mtime,
                        "start_line": 1,
                        "end_line": 1,
                        "start_byte": 0,
                        "end_byte": len("def filename_shortcut(): pass\n"),
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

            rg_result = {
                "path": str(indexed_file),
                "file": str(indexed_file),
                "chunk": "def filename_shortcut(): pass",
                "snippet": "def filename_shortcut(): pass",
                "language": "python",
                "start_line": 1,
                "end_line": 1,
                "score": 1.0,
                "fallback": "rg-shortcut",
            }
            semantic_file = root / "src" / "semantic.py"
            semantic_file.write_text("def semantic_answer(): pass\n")
            semantic_result = {
                "path": str(semantic_file),
                "file": str(semantic_file),
                "chunk": "def semantic_answer(): pass",
                "snippet": "def semantic_answer(): pass",
                "language": "python",
                "start_line": 1,
                "end_line": 1,
                "score": 0.8,
            }
            decision = cli_module.RouterDecision(
                intent="mixed",
                primary_token="",
                skip_cascade=False,
                skip_filename=False,
                skip_lexical=False,
                confidence=0.75,
                source="llm",
                reason="literal token",
                out_of_scope="none",
            )

            class _FakeEmbedder:
                def embed(self, text):
                    return [1.0, 0.0]

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
                cli_module.auto_index, "incremental_refresh", return_value=0
            ), patch.object(
                cli_module.auto_index, "filename_shortcut", return_value=None
            ), patch.object(
                cli_module.auto_index, "lexical_shortcut", return_value=[rg_result]
            ), patch.object(
                cli_module, "_symbols_table_populated", return_value=True
            ), patch.object(
                cli_module.code_graph, "populate_graph_table", return_value=0
            ), patch.object(
                cli_module, "get_embedder", return_value=_FakeEmbedder()
            ) as get_embedder_mock, patch.object(
                cli_module, "get_answerer", return_value=object()
            ), patch.object(
                cli_module, "maybe_start_recovery", return_value=None
            ), patch.object(
                cli_module,
                "cascade_search",
                return_value=(
                    [semantic_result],
                    {
                        "path": "cosine-cheap",
                        "early_exit": True,
                        "gap": 1.0,
                        "tau": 0.1,
                    },
                ),
            ):
                result = runner.invoke(
                    cli_module.cli,
                    ["search", "filename_shortcut", "--json"],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(get_embedder_mock.called, "cascade should still run")
        payload = json.loads(result.output)
        paths = {item["path"] for item in payload}
        self.assertIn(str(indexed_file), paths)
        self.assertIn(str(semantic_file), paths)


if __name__ == "__main__":
    unittest.main()
