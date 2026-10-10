# SPDX-License-Identifier: Apache-2.0
"""Compact agent payloads, declaration outlines and the `symbols` command."""

from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from click.testing import CliRunner

from skylakegrep.src import agent_payload as ap
from skylakegrep.src import cli as cli_module


PAGINATOR = textwrap.dedent(
    '''\
    import collections.abc
    from math import ceil

    from django.utils.translation import gettext_lazy as _


    class InvalidPage(Exception):
        pass


    class Paginator:
        """Split a long list of items into numbered pages."""

        def __init__(self, object_list, per_page, orphans=0):
            self.object_list = object_list
            self.per_page = int(per_page)

        def validate_number(self, number):
            if number < 1:
                raise InvalidPage("page number below one")
            return number

        def page(self, number):
            number = self.validate_number(number)
            bottom = (number - 1) * self.per_page
            return self.object_list[bottom : bottom + self.per_page]

        @property
        def num_pages(self):
            return ceil(len(self.object_list) / self.per_page)
    '''
)


def _full_result(root: Path) -> dict:
    path = str(root / "pkg" / "paginator.py")
    return {
        "path": path,
        "start_line": 12,
        "end_line": 30,
        "language": "python",
        "score": 1.234567,
        "snippet": "[file: pkg/paginator.py] [lang: python] [symbol: Paginator]\n\n" + PAGINATOR,
        "fallback": "candidate-recall",
        "candidate_recall": True,
        "candidate_recall_lanes": ["chunk", "symbol"],
        "source_type": "source",
        "search_intent": "semantic",
        "evidence_terms": ["paginator", "page"],
        "why_ranked": {"lanes": ["chunk", "symbol"], "covered_terms": ["paginator", "page"], "score": 1.23},
        "evidence_bundle": {
            "primary_anchor": {"path": path, "start_line": 12, "end_line": 30},
            "supporting_chunks": [{"path": path, "start_line": 40, "end_line": 44, "score": 0.9}],
            "why_ranked": {"lanes": ["chunk", "symbol"]},
        },
        "supporting_chunks": [{"path": path, "start_line": 40, "end_line": 44, "score": 0.9}],
        "confidence": 0.8,
        "agent_summary": {
            "quality": "best",
            "confidence": 0.8,
            "confidence_basis": "lexical-and-retrieval",
            "likely_files": [path, str(root / "pkg" / "views.py")],
            "primary_anchor": path,
            "missing_signal": "",
            "suggested_followup_probe": f"skygrep --agent-context --include '{root}/pkg' 'paginate'",
            "covered_terms": ["paginator"],
            "recall_paths": 12,
        },
    }


class CompactResultsTests(unittest.TestCase):
    def test_compact_keeps_agent_keys_and_drops_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            full = _full_result(root)
            [item] = ap.compact_results([full], root=root, query="split a list into pages")
        self.assertEqual(item["path"], os.path.join("pkg", "paginator.py"))
        self.assertEqual((item["start_line"], item["end_line"]), (12, 30))
        self.assertEqual(item["score"], 1.235)
        self.assertEqual(item["evidence_terms"], ["paginator", "page"])
        self.assertEqual(item["lanes"], ["chunk", "symbol"])
        self.assertEqual(item["also"], [[40, 44]])
        for dropped in ("evidence_bundle", "why_ranked", "language", "fallback",
                        "candidate_recall", "search_intent", "supporting_chunks"):
            self.assertNotIn(dropped, item)
        summary = item["agent_summary"]
        self.assertEqual(summary["quality"], "best")
        # Files already shown as results are not repeated in likely_files.
        self.assertEqual(summary["likely_files"], [os.path.join("pkg", "views.py")])
        self.assertNotIn(str(root), summary["suggested_followup_probe"])

    def test_compact_payload_is_much_smaller_than_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            results = [_full_result(root) for _ in range(5)]
            legacy = json.dumps(results, indent=2)
            compact = ap.dumps(ap.compact_results(results, root=root, query="pages"))
        self.assertLess(len(compact) * 2, len(legacy))

    def test_strict_verification_survives(self):
        r = _full_result(Path("/tmp"))
        r["strict_verification"] = {"status": "passed"}
        [item] = ap.compact_results([r], root=None)
        self.assertEqual(item["strict_verification"], {"status": "passed"})


class TrimSnippetTests(unittest.TestCase):
    def test_short_snippet_only_loses_chunk_header(self):
        out = ap.trim_snippet("[file: a.py] [lang: python] [symbol: f]\n\ndef f():\n    return 1", ["f"], 1200)
        self.assertEqual(out, "[symbol: f]\ndef f():\n    return 1")

    def test_long_snippet_keeps_declaration_and_term_lines(self):
        body = "def outer():\n" + "\n".join(f"    filler_{i} = {i}" for i in range(200))
        body += "\n    token = refresh_token(claims)\n" + "\n".join(f"    tail_{i} = {i}" for i in range(50))
        out = ap.trim_snippet(body, ["refresh"], 400)
        self.assertTrue(out.startswith("def outer():"))
        self.assertIn("refresh_token(claims)", out)
        self.assertIn("⋯", out)
        self.assertLessEqual(len(out), 420)


class OutlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "pkg").mkdir()
        self.file = self.root / "pkg" / "paginator.py"
        self.file.write_text(PAGINATOR)

    def tearDown(self):
        self.tmp.cleanup()

    def test_outline_lists_declarations_in_file_order_without_imports(self):
        groups = ap.term_groups("which component splits items into numbered pages", root=self.root)
        result = ap.outline_file(self.file, groups, budget_chars=4000)
        rows = [line for _, line in result["lines"]]
        self.assertIn("class Paginator:", rows)
        self.assertIn("def page(self, number):", rows)
        self.assertFalse(any(r.startswith(("import", "from")) for r in rows))
        numbers = [n for n, _ in result["lines"]]
        self.assertEqual(numbers, sorted(numbers))

    def test_budget_prefers_query_matching_declarations(self):
        groups = ap.term_groups("validate page number", root=self.root)
        result = ap.outline_file(self.file, groups, budget_chars=60)
        rows = [line for _, line in result["lines"]]
        # The declaration hitting the most query concepts wins the budget.
        self.assertIn("def validate_number(self, number):", rows)
        self.assertNotIn("def __init__(self, object_list, per_page, orphans=0):", rows)
        self.assertTrue(result["truncated"])

    def test_plain_mentions_need_two_concepts(self):
        groups = ap.term_groups("object list per page", root=self.root)
        rows = [line for _, line in ap.outline_file(self.file, groups)["lines"]]
        # "self.per_page = int(per_page)" mentions only one concept.
        self.assertNotIn("self.per_page = int(per_page)", rows)
        # This mentions "object", "list", "page" (three concepts).
        self.assertIn("return self.object_list[bottom : bottom + self.per_page]", rows)

    def test_repository_name_is_not_a_query_term(self):
        root = self.root / "django"
        root.mkdir()
        self.assertNotIn("django", ap.query_terms("where does django paginate", root=root))

    def test_outline_files_skips_non_source_and_respects_max_files(self):
        (self.root / "README.md").write_text("# pages\n")
        for name in ("a.py", "b.py", "c.py", "d.py"):
            (self.root / name).write_text("def page():\n    pass\n")
        out = ap.outline_files(
            ["README.md", "a.py", "b.py", "c.py", "d.py"], "page", root=self.root, max_files=3
        )
        self.assertEqual([o["path"] for o in out], ["a.py", "b.py", "c.py"])


class _chdir:
    def __init__(self, path):
        self.path, self.prev = path, None

    def __enter__(self):
        self.prev = os.getcwd()
        os.chdir(self.path)

    def __exit__(self, *exc):
        os.chdir(self.prev)


class SymbolsCommandTests(unittest.TestCase):
    def test_symbols_text_and_json(self):
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as d, _chdir(d):
            Path("pkg").mkdir()
            Path("pkg/paginator.py").write_text(PAGINATOR)
            text = runner.invoke(cli_module.cli, ["symbols", "pkg/paginator.py", "missing.py", "-q", "numbered pages"])
            self.assertEqual(text.exit_code, 0, text.output)
            self.assertIn("## pkg/paginator.py", text.output)
            self.assertIn(": class Paginator:", text.output)
            self.assertIn("## missing.py (not found)", text.output)
            raw = runner.invoke(cli_module.cli, ["symbols", "--json", "pkg/paginator.py", "-q", "pages"])
            self.assertEqual(raw.exit_code, 0, raw.output)
            payload = json.loads(raw.output)
            self.assertEqual(payload["outline"][0]["path"], os.path.join("pkg", "paginator.py"))
            self.assertNotIn("\n", raw.output.strip())

    def test_symbols_is_a_subcommand_not_a_query(self):
        self.assertIn("symbols", cli_module._SUBCOMMANDS)


class RenderContractTests(unittest.TestCase):
    def tearDown(self):
        cli_module._AGENT_RENDER.set(None)

    def test_full_style_is_legacy_pretty_json(self):
        cli_module._AGENT_RENDER.set(None)
        out = cli_module.render_json_results([_full_result(Path("/tmp"))])
        self.assertIn('\n  {\n    "path"', out)
        self.assertIn("evidence_bundle", out)

    def test_compact_style(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cli_module._AGENT_RENDER.set({"style": "compact", "query": "pages", "display_root": root})
            out = cli_module.render_json_results([_full_result(root)], include_snippet=False)
        payload = json.loads(out)
        self.assertIsInstance(payload, list)
        self.assertNotIn("snippet", payload[0])
        self.assertNotIn("evidence_bundle", payload[0])

    def test_slim_style_outlines_top_source_files_and_points_to_next(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "pkg").mkdir()
            results = []
            for i in range(5):
                f = root / "pkg" / f"m{i}.py"
                f.write_text(f"class Pager{i}:\n    def page(self):\n        pass\n")
                r = _full_result(root)
                r["path"] = str(f)
                results.append(r)
            cli_module._AGENT_RENDER.set({"style": "slim", "query": "pages", "display_root": root})
            payload = json.loads(cli_module.render_json_results(results, include_snippet=False))
        self.assertEqual(len(payload["results"]), 5)
        self.assertEqual(len(payload["outline"]), cli_module.SLIM_OUTLINE_FILES)
        self.assertTrue(payload["next"].startswith("skygrep symbols pkg/m3.py pkg/m4.py"))


if __name__ == "__main__":
    unittest.main()
