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

    def test_search_command_name_is_explicit(self):
        self.assertIn("search", cli_module.cli.commands)
        self.assertNotIn("search-cmd", cli_module.cli.commands)

    def test_help_flag_does_not_route_to_search(self):
        runner = CliRunner()
        result = runner.invoke(cli_module.cli, ["--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Common usage", result.output)

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
        self.assertIn("proactive filename searching configured roots", result.output)
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
