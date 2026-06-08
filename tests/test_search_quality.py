import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from skylakegrep.src import cli as cli_module
from skylakegrep.src.candidate_recall import (
    build_agent_context_results,
    candidate_chunk_results,
    recall_candidate_paths,
)
from skylakegrep.src.indexer import collect_indexable_files, prepare_file_chunks
from skylakegrep.src.intent import merge_results as merge_tiers
from skylakegrep.src.storage import init_db, path_matches, populate_file_embeddings, search, store_chunks_batch


class StaticEmbedder:
    def embed(self, text: str) -> list[float]:
        if "token" in text.lower():
            return [1.0, 0.0]
        return [0.0, 1.0]


class SearchQualityTests(unittest.TestCase):
    def test_agent_presets_disable_llm_router_by_default(self):
        self.assertFalse(
            cli_module._effective_llm_router_for_agent_mode(
                "fast",
                True,
                llm_router_explicit=False,
            )
        )
        self.assertFalse(
            cli_module._effective_llm_router_for_agent_mode(
                "context",
                True,
                llm_router_explicit=False,
            )
        )
        self.assertTrue(
            cli_module._effective_llm_router_for_agent_mode(
                "context",
                True,
                llm_router_explicit=True,
            )
        )
        self.assertTrue(
            cli_module._effective_llm_router_for_agent_mode(
                "off",
                True,
                llm_router_explicit=False,
            )
        )

    def test_agent_fast_and_context_disable_cascade_by_default(self):
        self.assertFalse(
            cli_module._effective_cascade_for_agent_mode(
                "fast",
                True,
                cascade_explicit=False,
            )
        )
        self.assertFalse(
            cli_module._effective_cascade_for_agent_mode(
                "context",
                True,
                cascade_explicit=False,
            )
        )
        self.assertTrue(
            cli_module._effective_cascade_for_agent_mode(
                "context",
                True,
                cascade_explicit=True,
            )
        )
        self.assertTrue(
            cli_module._effective_cascade_for_agent_mode(
                "deep",
                True,
                cascade_explicit=False,
            )
        )

    def test_relative_include_glob_matches_absolute_index_path(self):
        path = "/tmp/project/src/auth/session.py"

        self.assertTrue(path_matches(path, ("src/**",), ()))
        self.assertTrue(path_matches(path, ("src/auth/*.py",), ()))
        self.assertFalse(path_matches(path, ("docs/**",), ()))

    def test_prepare_file_chunks_records_line_ranges_when_parser_falls_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "main.go"
            path.write_text(
                "package main\n\n"
                "func greet() {\n"
                "    println(\"hello\")\n"
                "}\n",
                encoding="utf-8",
            )

            with patch("skylakegrep.src.indexer.get_parser", return_value=None):
                chunks = prepare_file_chunks(path)

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["start_line"], 1)
        self.assertEqual(chunks[0]["end_line"], 5)
        self.assertEqual(chunks[0]["start_byte"], 0)
        self.assertGreater(chunks[0]["end_byte"], chunks[0]["start_byte"])

    def test_code_chunking_supplements_large_module_level_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.py"
            body = "\n".join(
                [
                    "SNIPPET_BODY = \"\"\"",
                    "Use --content when the caller needs matched snippets.",
                    "Use --detail full after narrowing to one file.",
                    "Use --json for machine-readable agent context.",
                    "Use --answer only for synthesized local answers.",
                    "\"\"\"",
                    "",
                    "def helper():",
                    "    return True",
                ]
            )
            path.write_text(body, encoding="utf-8")

            chunks = prepare_file_chunks(path, root=Path(temp_dir))

        joined = "\n\n".join(chunk["chunk"] for chunk in chunks)
        self.assertIn("SNIPPET_BODY", joined)
        self.assertIn("--detail full", joined)
        self.assertIn("def helper", joined)

    def test_collect_indexable_files_honors_gitignore_and_default_vendor_dirs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".gitignore").write_text("ignored.py\nbuild/\n", encoding="utf-8")
            (root / "kept.py").write_text("print('kept')\n", encoding="utf-8")
            (root / "ignored.py").write_text("print('ignored')\n", encoding="utf-8")
            (root / "build").mkdir()
            (root / "build" / "generated.py").write_text("print('generated')\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "vendor.py").write_text("print('vendor')\n", encoding="utf-8")

            files = {path.relative_to(root).as_posix() for path in collect_indexable_files(root)}

        self.assertEqual(files, {"kept.py"})

    def test_search_deduplicates_same_logical_result_and_returns_provenance(self):
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
                        "start_line": 10,
                        "end_line": 11,
                        "start_byte": 100,
                        "end_byte": 160,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": "auth.py",
                        "chunk": "def validate_token(token):\n    return token.startswith('token-')",
                        "language": "python",
                        "chunk_index": 1,
                        "file_mtime": 1.0,
                        "start_line": 10,
                        "end_line": 11,
                        "start_byte": 100,
                        "end_byte": 160,
                        "embedding": [1.0, 0.0],
                    },
                ],
            )

            results = search(conn, [1.0, 0.0], top_k=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["path"], "auth.py")
        self.assertEqual(results[0]["start_line"], 10)
        self.assertEqual(results[0]["end_line"], 11)
        self.assertIn("validate_token", results[0]["snippet"])

    def test_include_scope_overrides_multi_resolution_file_shortlist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            conn = init_db(Path(temp_dir) / "index.db")
            store_chunks_batch(
                conn,
                [
                    {
                        "file": "docs/target.md",
                        "chunk": "The scoped file contains the deployment checklist.",
                        "language": "markdown",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 1,
                        "start_byte": 0,
                        "end_byte": 52,
                        "embedding": [0.0, 1.0],
                    },
                    {
                        "file": "src/high_semantic.py",
                        "chunk": "unrelated but semantically high",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 1,
                        "start_byte": 0,
                        "end_byte": 31,
                        "embedding": [1.0, 0.0],
                    },
                ],
            )
            populate_file_embeddings(conn)

            results = search(
                conn,
                [1.0, 0.0],
                top_k=5,
                include_patterns=("docs/**",),
                multi_resolution=True,
                file_top=1,
            )

        self.assertEqual([result["path"] for result in results], ["docs/target.md"])

    def test_candidate_recall_finds_paths_from_independent_cheap_lanes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "src" / "session_refresh.py"
            target.parent.mkdir()
            target.write_text("def refresh_session():\n    return 'ok'\n", encoding="utf-8")
            conn = init_db(root / "index.db")
            store_chunks_batch(
                conn,
                [
                    {
                        "file": str(target),
                        "chunk": "def refresh_session():\n    return 'ok'",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 39,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": str(root / "docs" / "overview.md"),
                        "chunk": "general overview",
                        "language": "markdown",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 1,
                        "start_byte": 0,
                        "end_byte": 16,
                        "embedding": [0.0, 1.0],
                    },
                ],
            )

            paths, telemetry = recall_candidate_paths(
                conn,
                "where does session refresh logic live?",
                root,
                include_patterns=("src/**",),
                rg_timeout=0.5,
            )

        self.assertIn(str(target), paths)
        self.assertGreaterEqual(telemetry["total_paths"], 1)
        self.assertTrue(set(telemetry["path_lanes"][str(target)]))

    def test_candidate_recall_extracts_best_lexical_evidence_chunk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "src" / "session_refresh.py"
            target.parent.mkdir()
            conn = init_db(root / "index.db")
            store_chunks_batch(
                conn,
                [
                    {
                        "file": str(target),
                        "chunk": "module header only",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 1,
                        "start_byte": 0,
                        "end_byte": 18,
                        "embedding": [0.0, 1.0],
                    },
                    {
                        "file": str(target),
                        "chunk": "def refresh_session():\n    return 'refresh complete'",
                        "language": "python",
                        "chunk_index": 1,
                        "file_mtime": 1.0,
                        "start_line": 20,
                        "end_line": 21,
                        "start_byte": 100,
                        "end_byte": 150,
                        "embedding": [1.0, 0.0],
                    },
                ],
            )

            results = candidate_chunk_results(
                conn,
                "where does session refresh logic live?",
                {str(target)},
                top_k=5,
            )

        self.assertEqual(results[0]["path"], str(target))
        self.assertIn("refresh_session", results[0]["snippet"])

    def test_candidate_recall_bundles_supporting_symbol_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "src" / "ui.py"
            target.parent.mkdir()
            conn = init_db(root / "index.db")
            store_chunks_batch(
                conn,
                [
                    {
                        "file": str(target),
                        "chunk": (
                            "[file: src/ui.py] [lang: python]\n"
                            "HELIX_ROLE_FRAMES = ('v d M', 'w d m')"
                        ),
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
                        "file": str(target),
                        "chunk": (
                            "[file: src/ui.py] [lang: python] [symbol: block]\n"
                            "def block(text):\n"
                            "    \"\"\"Attach the workflow rail to rendered output.\"\"\""
                        ),
                        "language": "python",
                        "chunk_index": 1,
                        "file_mtime": 1.0,
                        "start_line": 20,
                        "end_line": 22,
                        "start_byte": 100,
                        "end_byte": 180,
                        "embedding": [0.0, 1.0],
                    },
                    {
                        "file": str(target),
                        "chunk": (
                            "[file: src/ui.py] [lang: python] [symbol: helix_frame]\n"
                            "def helix_frame(index):\n"
                            "    return HELIX_ROLE_FRAMES[index]"
                        ),
                        "language": "python",
                        "chunk_index": 2,
                        "file_mtime": 1.0,
                        "start_line": 30,
                        "end_line": 32,
                        "start_byte": 200,
                        "end_byte": 260,
                        "embedding": [0.0, 1.0],
                    },
                ],
            )

            results = candidate_chunk_results(
                conn,
                "where is the terminal helix workflow rail rendered?",
                {str(target)},
                top_k=5,
            )

        self.assertEqual(results[0]["path"], str(target))
        self.assertIn("workflow rail", results[0]["snippet"])
        self.assertIn("HELIX_ROLE_FRAMES", results[0]["snippet"])
        self.assertIn("helix_frame", results[0]["snippet"])
        self.assertIn("supporting_chunks", results[0])

    def test_candidate_recall_prefers_path_specific_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            harness = root / "src" / "harness_flow.rs"
            topology = root / "src" / "worker_topology" / "architecture_control.rs"
            harness.parent.mkdir(parents=True)
            topology.parent.mkdir(parents=True)
            conn = init_db(root / "index.db")
            store_chunks_batch(
                conn,
                [
                    {
                        "file": str(harness),
                        "chunk": (
                            "[file: src/harness_flow.rs] [lang: rust] "
                            "[symbol: HarnessFlowStatus]\n"
                            "provider harness status records"
                        ),
                        "language": "rust",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 80,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": str(topology),
                        "chunk": (
                            "[file: src/worker_topology/architecture_control.rs] "
                            "[lang: rust] [symbol: AdaptationTriggerDecision]\n"
                            "adaptive harness provider architecture control"
                        ),
                        "language": "rust",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 110,
                        "embedding": [0.0, 1.0],
                    },
                ],
            )

            results = candidate_chunk_results(
                conn,
                "WorkerTopology Adaptive Harness provider",
                {str(harness), str(topology)},
                top_k=2,
                path_scores={
                    str(harness): 1.4,
                    str(topology): 1.6,
                },
            )

        self.assertEqual(results[0]["path"], str(topology))

    def test_agent_context_test_intent_prefers_tests_over_release_notes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            test_path = root / "tests" / "test_search_quality.py"
            doc_path = root / "docs" / "skylakegrep-0.5.0.md"
            test_path.parent.mkdir(parents=True)
            doc_path.parent.mkdir(parents=True)
            conn = init_db(root / "index.db")
            store_chunks_batch(
                conn,
                [
                    {
                        "file": str(test_path),
                        "chunk": (
                            "def test_stable_json_output_schema():\n"
                            "    assert payload[0]['path']"
                        ),
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 80,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": str(doc_path),
                        "chunk": (
                            "JSON output schema unchanged. Stable JSON output schema "
                            "documented in release notes."
                        ),
                        "language": "markdown",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 1,
                        "end_line": 2,
                        "start_byte": 0,
                        "end_byte": 90,
                        "embedding": [0.0, 1.0],
                    },
                ],
            )

            results, telemetry = build_agent_context_results(
                conn,
                "Which test covers stable JSON output schema?",
                root,
                top_k=2,
            )

        self.assertEqual(telemetry["intent"], "test_location")
        self.assertEqual(results[0]["path"], str(test_path))
        self.assertEqual(results[0]["source_type"], "test")
        self.assertEqual(results[0]["agent_summary"]["quality"], "best")

    def test_symbol_anchor_prefers_matching_function_body_chunk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            indexer = root / "skylakegrep" / "src" / "indexer.py"
            indexer.parent.mkdir(parents=True)
            conn = init_db(root / "index.db")
            store_chunks_batch(
                conn,
                [
                    {
                        "file": str(indexer),
                        "chunk": "def extract_code_chunks():\n    chunks = []\n    language = 'python'",
                        "language": "python",
                        "chunk_index": 0,
                        "file_mtime": 1.0,
                        "start_line": 10,
                        "end_line": 12,
                        "start_byte": 0,
                        "end_byte": 70,
                        "embedding": [1.0, 0.0],
                    },
                    {
                        "file": str(indexer),
                        "chunk": (
                            "for i, chunk in enumerate(chunks):\n"
                            "    results.append({'language': lang, 'file_mtime': file_mtime})"
                        ),
                        "language": "python",
                        "chunk_index": 1,
                        "file_mtime": 1.0,
                        "start_line": 48,
                        "end_line": 52,
                        "start_byte": 80,
                        "end_byte": 180,
                        "embedding": [0.0, 1.0],
                    },
                ],
            )
            conn.execute(
                """
                INSERT INTO symbols(file, name, name_lower, kind, start_line, end_line, file_mtime)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(indexer),
                    "prepare_file_chunks",
                    "prepare file chunks",
                    "function",
                    44,
                    54,
                    1.0,
                ),
            )
            conn.commit()

            results, _ = build_agent_context_results(
                conn,
                "Where are per-file chunks prepared with language and mtime metadata?",
                root,
                top_k=1,
            )

        self.assertEqual(results[0]["path"], str(indexer))
        self.assertEqual(results[0]["start_line"], 48)
        self.assertIn("prepare_file_chunks", results[0]["symbol_anchor_names"])

    def test_candidate_path_recall_ignores_absolute_project_name_noise(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "sample-code"
            src = root / "src"
            src.mkdir(parents=True)
            topology = src / "worker_topology" / "architecture_control.rs"
            conn = init_db(root / "index.db")
            chunks = []
            for index in range(12):
                path = src / f"a_generic_{index:02d}.rs"
                chunks.append({
                    "file": str(path),
                    "chunk": "provider harness generic",
                    "language": "rust",
                    "chunk_index": index,
                    "file_mtime": 1.0,
                    "start_line": 1,
                    "end_line": 1,
                    "start_byte": 0,
                    "end_byte": 24,
                    "embedding": [1.0, 0.0],
                })
            chunks.append({
                "file": str(topology),
                "chunk": "adaptive harness provider architecture control",
                "language": "rust",
                "chunk_index": 99,
                "file_mtime": 1.0,
                "start_line": 1,
                "end_line": 1,
                "start_byte": 0,
                "end_byte": 46,
                "embedding": [0.0, 1.0],
            })
            store_chunks_batch(conn, chunks)

            cands, _ = recall_candidate_paths(
                conn,
                "Skylake WorkerTopology Adaptive Harness provider",
                src,
                include_patterns=("src/**",),
                max_paths=20,
                rg_timeout=0.001,
            )

        self.assertIn(str(topology), cands)

    def test_candidate_recall_ties_with_semantic_for_semantic_intent(self):
        ranked = merge_tiers(
            filename=[],
            lexical=[],
            semantic=[
                {"path": "src/wrong.py", "score": 0.1, "fallback": "cascade"},
                {
                    "path": "src/right.py",
                    "score": 0.9,
                    "fallback": "candidate-recall",
                },
            ],
            intent="semantic",
            top_k=1,
        )

        self.assertEqual(ranked[0]["path"], "src/right.py")

    def test_search_json_outputs_stable_result_schema(self):
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
            runner = CliRunner()
            old_db_path = os.environ.get("SKYGREP_DB_PATH")
            os.environ["SKYGREP_DB_PATH"] = str(db_path)
            try:
                with patch.object(cli_module, "get_embedder", return_value=StaticEmbedder()):
                    result = runner.invoke(cli_module.cli, ["search", "--no-lexical-prefilter", "token", "--json", "--no-rerank"])
            finally:
                if old_db_path is None:
                    os.environ.pop("SKYGREP_DB_PATH", None)
                else:
                    os.environ["SKYGREP_DB_PATH"] = old_db_path

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(
            sorted(payload[0].keys()),
            ["end_line", "language", "path", "score", "snippet", "start_line"],
        )
        self.assertEqual(payload[0]["path"], "auth.py")
        self.assertEqual(payload[0]["start_line"], 3)

    def test_machine_json_filters_low_score_results_without_query_evidence(self):
        results = [
            {
                "path": "src/recovery.py",
                "start_line": 1,
                "end_line": 4,
                "language": "python",
                "score": 0.43,
                "snippet": "background implementation loop recovery state machine",
            }
        ]

        filtered = cli_module._filter_low_evidence_machine_results(
            results,
            "where is the kubernetes autoscaler reconciliation loop implemented",
            min_score=0.50,
        )

        self.assertEqual(filtered, [])

    def test_machine_json_keeps_low_score_results_with_direct_evidence(self):
        results = [
            {
                "path": "src/router.py",
                "start_line": 1,
                "end_line": 4,
                "language": "python",
                "score": 0.43,
                "snippet": "router timeout is applied before the local model call",
            }
        ]

        filtered = cli_module._filter_low_evidence_machine_results(
            results,
            "where is local router timeout applied",
            min_score=0.50,
        )

        self.assertEqual(filtered, results)


if __name__ == "__main__":
    unittest.main()
