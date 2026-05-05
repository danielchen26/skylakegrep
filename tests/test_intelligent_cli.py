"""Tests for the intelligent-CLI module shipped in 0.2.4.

Covers the four user-visible behaviours: out-of-scope query
detection, typo correction for unknown flags, low-confidence result
hints, and the first-run nudge gate. Each test is deliberately a
minimal contract test — the actual suggestion text can evolve, but
the trigger conditions ("does this kind of query fire the hint")
must stay stable across refactors so the user experience doesn't
regress silently.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from skylakegrep.src.intelligent_cli import (
    assess_result_quality,
    closest_match,
    detect_out_of_scope,
    mark_first_run_nudge_shown,
    render_first_run_nudge,
    render_out_of_scope_hint,
    should_show_first_run_nudge,
    suggest_for_unknown_command,
    suggest_for_unknown_option,
)
from skylakegrep.src.storage import init_db


class OutOfScopeDetectionTests(unittest.TestCase):
    """Out-of-scope queries are metadata queries (mtime / size /
    listing) that semantic search can't answer. We must catch them
    without flagging legitimate semantic queries."""

    def test_chinese_recency_query_is_flagged(self):
        hint = detect_out_of_scope("我最近工作上的十个文件")
        self.assertIsNotNone(hint)
        self.assertIn("git log", hint["suggested_command"])

    def test_chinese_day_relative_recency_is_flagged(self):
        # Regression: 0.2.4 missed "昨天" (yesterday) and similar
        # day-relative recency tokens because the metadata-token list
        # only covered "最近" / "最新" / "近期".
        for q in [
            "我昨天打开过的十个文件",
            "今天写过的代码",
            "前天的pdf",
            "上周改过的文件",
            "本周编辑过的笔记",
        ]:
            with self.subTest(query=q):
                self.assertIsNotNone(
                    detect_out_of_scope(q),
                    msg=f"day-relative recency query {q!r} should be flagged",
                )

    def test_english_day_relative_recency_is_flagged(self):
        # "what I worked on today" is intentionally NOT in this list:
        # the "what" interrogative is genuinely ambiguous (could be
        # asking for a list, could be asking semantically), and the
        # safer fallback is to let it run as a content query rather
        # than nag the user with a hint that may not apply.
        for q in [
            "files I opened yesterday",
            "today opened",
            "this week's edits",
        ]:
            with self.subTest(query=q):
                self.assertIsNotNone(detect_out_of_scope(q))

    def test_english_recency_query_is_flagged(self):
        hint = detect_out_of_scope("recent files I changed")
        self.assertIsNotNone(hint)
        self.assertIn("git log", hint["suggested_command"])

    def test_size_query_is_flagged(self):
        hint = detect_out_of_scope("largest files in the repo")
        self.assertIsNotNone(hint)
        self.assertIn("size", hint["reason"])

    def test_listing_query_is_flagged(self):
        hint = detect_out_of_scope("list all files")
        self.assertIsNotNone(hint)

    def test_semantic_query_with_metadata_word_is_not_flagged(self):
        # "where is the recent change to auth flow" mentions "recent"
        # but is asking about a specific behaviour — semantic.
        for q in [
            "where is the recent change to auth flow",
            "how does the latest implementation handle errors",
            "what function handles the largest payload",
            "explain how all the routes connect",
        ]:
            with self.subTest(query=q):
                self.assertIsNone(detect_out_of_scope(q),
                                  msg=f"semantic query {q!r} false-flagged")

    def test_pure_semantic_query_is_not_flagged(self):
        for q in [
            "how does auth refresh work",
            "where is useState defined",
            "implementation of cascade tau threshold",
            "what is the LLM router doing",
        ]:
            with self.subTest(query=q):
                self.assertIsNone(detect_out_of_scope(q))

    def test_long_query_is_not_flagged(self):
        # Long queries are content queries by definition; even if they
        # mention a metadata token, we don't flag.
        q = (
            "list all the places where the cascade decides "
            "to early-exit and the σ-gap is below the floor"
        )
        self.assertIsNone(detect_out_of_scope(q))

    def test_render_includes_suggestion(self):
        hint = detect_out_of_scope("最近修改的文件")
        rendered = render_out_of_scope_hint(hint, "最近修改的文件")
        self.assertIn("git log", rendered)
        self.assertIn("最近修改的文件", rendered)
        self.assertIn("SKYGREP_NO_HINTS", rendered)


class TypoCorrectionTests(unittest.TestCase):
    """``difflib`` cutoff 0.6 catches one-or-two-character edits
    without firing on completely-different strings."""

    def test_close_typo_returns_match(self):
        self.assertEqual(closest_match("tup", ["top", "json", "agentic"]), "top")
        self.assertEqual(closest_match("sematic-only", ["semantic-only", "agentic"]), "semantic-only")

    def test_far_typo_returns_none(self):
        self.assertIsNone(closest_match("xyz", ["top", "json"]))

    def test_unknown_option_suggestion_format(self):
        sug = suggest_for_unknown_option("--tup", ["--top", "--json"])
        self.assertIsNotNone(sug)
        self.assertIn("--top", sug)
        self.assertIn("--tup", sug)

    def test_unknown_option_too_far_returns_none(self):
        self.assertIsNone(suggest_for_unknown_option("--zzzz", ["--top", "--json"]))

    def test_unknown_command_suggestion_format(self):
        sug = suggest_for_unknown_command("serach", ["search", "doctor", "stats"])
        self.assertIsNotNone(sug)
        self.assertIn("search", sug)


class ResultQualityHintTests(unittest.TestCase):
    """Low-confidence hint fires when both top-1 score and σ-gap
    are below the noise floor. Good results stay quiet."""

    def test_good_results_no_hint(self):
        self.assertIsNone(
            assess_result_quality([{"score": 0.85}], {"gap": 0.04})
        )

    def test_low_top1_low_gap_fires_hint(self):
        hint = assess_result_quality([{"score": 0.18}], {"gap": 0.001})
        self.assertIsNotNone(hint)
        self.assertIn("σ-gap", hint)

    def test_empty_results_fires_hint(self):
        hint = assess_result_quality([], None)
        self.assertIsNotNone(hint)
        self.assertIn("doctor", hint)

    def test_low_top1_but_high_gap_no_hint(self):
        # Even if top1 is low, a high σ-gap means cosine is decisive
        # → trust it, don't surface a recovery menu.
        self.assertIsNone(
            assess_result_quality([{"score": 0.20}], {"gap": 0.10})
        )


class FirstRunNudgeTests(unittest.TestCase):
    """The nudge fires once-per-project. Tests cover the
    suppression logic via the metadata flag and the empty-chunks
    gate."""

    def _fresh_conn(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = Path(tmp.name)
        return init_db(path), path

    def test_fires_on_empty_index(self):
        conn, path = self._fresh_conn()
        try:
            self.assertTrue(should_show_first_run_nudge(conn))
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_silenced_after_mark(self):
        conn, path = self._fresh_conn()
        try:
            mark_first_run_nudge_shown(conn)
            self.assertFalse(should_show_first_run_nudge(conn))
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_silenced_by_env_var(self):
        conn, path = self._fresh_conn()
        try:
            with patch.dict(os.environ, {"SKYGREP_NO_HINTS": "1"}):
                self.assertFalse(should_show_first_run_nudge(conn))
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_render_mentions_doctor_and_setup(self):
        rendered = render_first_run_nudge()
        self.assertIn("doctor", rendered)
        self.assertIn("setup", rendered)


if __name__ == "__main__":
    unittest.main()
