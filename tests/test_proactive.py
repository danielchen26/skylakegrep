"""Tests for the 0.2.7 proactive enhancement framework.

Coverage spans the four contract guarantees that every enhancer
must honour:

  1. ``should_fire`` is consulted before scheduling — we don't
     pay the budget for enhancers that aren't going to run.
  2. The total wall-clock budget is enforced; over-budget
     enhancers are cancelled and their absence is reported in
     telemetry rather than masked.
  3. Failure isolation: one enhancer raising doesn't break the
     others.
  4. ``SKYGREP_NO_PROACTIVE=1`` and ``SKYGREP_NO_HINTS=1`` are
     master kill-switches that disable the runner entirely.

Plus regression coverage for the built-in ``filename_extend``
enhancer (token-extraction, directory injection, no-hits path).
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from skylakegrep.src import proactive as p
from skylakegrep.src.proactive import (
    ProactiveEnhancement,
    ProactiveResult,
    clear_registry,
    filename_extend_execute,
    filename_extend_should_fire,
    list_enhancers,
    register_enhancer,
    render_proactive_output,
    run_enhancers_parallel,
)


@dataclass
class _MockDecision:
    """Stand-in for the real ``llm_router.RouterDecision`` so we
    don't have to import the LLM machinery in unit tests."""

    intent: str = "mixed"
    primary_token: str = ""
    out_of_scope: object = None


class _Registry:
    """Context manager that snapshots and restores the global
    enhancer registry — every test starts and ends with the
    framework's built-in ``filename_extend`` enhancer registered."""

    def __enter__(self):
        self._saved = list(p._REGISTRY)
        clear_registry()
        return self

    def __exit__(self, *_a):
        p._REGISTRY[:] = self._saved


# ---------------------------------------------------------------------------
# Should-fire gate
# ---------------------------------------------------------------------------


class ShouldFireGateTests(unittest.TestCase):
    def test_no_eligible_enhancers_returns_empty(self):
        with _Registry():
            register_enhancer(ProactiveEnhancement(
                name="never",
                should_fire=lambda q, d, r: False,
                execute=lambda *a, **k: ProactiveResult("never"),
            ))
            res, tel = run_enhancers_parallel(
                "anything", _MockDecision(), [], top_k=10
            )
            self.assertEqual(res, [])
            self.assertEqual(tel["fired"], [])
            self.assertEqual(tel["completed"], [])

    def test_fires_only_when_gate_returns_true(self):
        with _Registry():
            calls: list[str] = []

            def fire(_q, _d, _r):
                return True

            def execute(_q, _d, _k, _b):
                calls.append("ran")
                return ProactiveResult("fires", note="x")

            register_enhancer(ProactiveEnhancement(
                name="fires",
                should_fire=fire,
                execute=execute,
                individual_budget_ms=200,
            ))
            res, tel = run_enhancers_parallel(
                "q", _MockDecision(), [], top_k=10
            )
            self.assertEqual(len(res), 1)
            self.assertEqual(calls, ["ran"])
            self.assertEqual(tel["fired"], ["fires"])

    def test_buggy_should_fire_treated_as_no(self):
        with _Registry():
            register_enhancer(ProactiveEnhancement(
                name="buggy",
                should_fire=lambda *a: 1 / 0,
                execute=lambda *a, **k: ProactiveResult("buggy"),
            ))
            res, tel = run_enhancers_parallel(
                "q", _MockDecision(), [], top_k=10
            )
            self.assertEqual(res, [])
            self.assertEqual(tel["fired"], [])


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


class BudgetEnforcementTests(unittest.TestCase):
    def test_slow_enhancer_is_cancelled_by_total_budget(self):
        """The runner's wall-clock must return within the budget
        even when an enhancer doesn't honour its individual budget.
        We use ``shutdown(wait=False, cancel_futures=True)`` so the
        slow thread keeps running in the background but the
        caller's elapsed time tracks the budget, not the enhancer."""

        with _Registry():
            def slow_execute(_q, _d, _k, _b):
                time.sleep(0.5)  # well past the test's 200 ms budget
                return ProactiveResult("slow")

            register_enhancer(ProactiveEnhancement(
                name="slow",
                should_fire=lambda *a: True,
                execute=slow_execute,
                individual_budget_ms=300,
            ))
            started = time.monotonic()
            res, tel = run_enhancers_parallel(
                "q", _MockDecision(), [], top_k=10,
                total_budget_ms=200,
            )
            elapsed_s = time.monotonic() - started
            # Wall-clock returns within ~budget + small overhead;
            # 350 ms ceiling leaves headroom for slow CI hosts but
            # is still much less than the slow enhancer's 0.5 s.
            self.assertLess(elapsed_s, 0.35)
            self.assertEqual(res, [])
            self.assertIn("slow", tel["timed_out"])

    def test_fast_enhancer_completes_within_budget(self):
        with _Registry():
            register_enhancer(ProactiveEnhancement(
                name="fast",
                should_fire=lambda *a: True,
                execute=lambda *a, **k: ProactiveResult(
                    "fast", note="quick win",
                ),
                individual_budget_ms=300,
            ))
            res, tel = run_enhancers_parallel(
                "q", _MockDecision(), [], top_k=10,
                total_budget_ms=500,
            )
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].enhancer_name, "fast")
            self.assertIn("fast", tel["completed"])
            self.assertEqual(tel["timed_out"], [])

    def test_zero_budget_disables_runner(self):
        with _Registry():
            register_enhancer(ProactiveEnhancement(
                name="never_runs",
                should_fire=lambda *a: True,
                execute=lambda *a, **k: ProactiveResult("never_runs"),
            ))
            res, tel = run_enhancers_parallel(
                "q", _MockDecision(), [], top_k=10,
                total_budget_ms=0,
            )
            self.assertEqual(res, [])
            self.assertEqual(tel["completed"], [])

    def test_partial_completion_under_pressure(self):
        """One fast + one slow ⇒ fast result returns, slow is timed-out."""

        with _Registry():
            register_enhancer(ProactiveEnhancement(
                name="fast",
                should_fire=lambda *a: True,
                execute=lambda *a, **k: ProactiveResult(
                    "fast", note="ok",
                ),
                individual_budget_ms=200,
            ))
            register_enhancer(ProactiveEnhancement(
                name="slow",
                should_fire=lambda *a: True,
                execute=lambda *a, **k: (time.sleep(2.0) or ProactiveResult("slow")),
                individual_budget_ms=300,
            ))
            res, tel = run_enhancers_parallel(
                "q", _MockDecision(), [], top_k=10,
                total_budget_ms=300,
            )
            names = [r.enhancer_name for r in res]
            self.assertIn("fast", names)
            self.assertNotIn("slow", names)
            self.assertIn("slow", tel["timed_out"])


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


class FailureIsolationTests(unittest.TestCase):
    def test_one_raising_enhancer_doesnt_break_others(self):
        with _Registry():
            register_enhancer(ProactiveEnhancement(
                name="raises",
                should_fire=lambda *a: True,
                execute=lambda *a, **k: (_ for _ in ()).throw(ValueError("x")),
                individual_budget_ms=200,
            ))
            register_enhancer(ProactiveEnhancement(
                name="ok",
                should_fire=lambda *a: True,
                execute=lambda *a, **k: ProactiveResult("ok", note="ok"),
                individual_budget_ms=200,
            ))
            res, tel = run_enhancers_parallel(
                "q", _MockDecision(), [], top_k=10,
            )
            names = [r.enhancer_name for r in res]
            self.assertEqual(names, ["ok"])
            # The raising enhancer is reported as fired but absent
            # from completed (silent drop).
            self.assertIn("raises", tel["fired"])
            self.assertIn("ok", tel["completed"])


# ---------------------------------------------------------------------------
# Master kill-switches
# ---------------------------------------------------------------------------


class KillSwitchTests(unittest.TestCase):
    def test_skygrep_no_proactive_disables(self):
        with _Registry(), patch.dict(
            os.environ, {"SKYGREP_NO_PROACTIVE": "1"}, clear=False
        ):
            register_enhancer(ProactiveEnhancement(
                name="should_be_disabled",
                should_fire=lambda *a: True,
                execute=lambda *a, **k: ProactiveResult("x"),
            ))
            res, tel = run_enhancers_parallel(
                "q", _MockDecision(), [], top_k=10
            )
            self.assertEqual(res, [])
            self.assertEqual(tel["fired"], [])

    def test_skygrep_no_hints_also_disables(self):
        with _Registry(), patch.dict(
            os.environ, {"SKYGREP_NO_HINTS": "1"}, clear=False
        ):
            register_enhancer(ProactiveEnhancement(
                name="should_be_disabled",
                should_fire=lambda *a: True,
                execute=lambda *a, **k: ProactiveResult("x"),
            ))
            res, tel = run_enhancers_parallel(
                "q", _MockDecision(), [], top_k=10
            )
            self.assertEqual(res, [])

    def test_default_is_enabled(self):
        # No env var → enabled. Use empty registry to avoid side
        # effects; the goal is to confirm the runner isn't
        # disabled by default.
        env = {k: v for k, v in os.environ.items()
               if k not in {"SKYGREP_NO_PROACTIVE", "SKYGREP_NO_HINTS"}}
        with _Registry(), patch.dict(os.environ, env, clear=True):
            self.assertFalse(p._proactive_disabled())


# ---------------------------------------------------------------------------
# filename_extend regression coverage
# ---------------------------------------------------------------------------


class FilenameExtendShouldFireTests(unittest.TestCase):
    """Per Principle 1, the gate trusts ``decision.intent`` only.
    We do NOT keyword-match the query against ``where is`` / ``在哪`` /
    etc. — that's the enumeration anti-pattern the user has flagged
    multiple times. The LLM router (or its rule-based fallback) is
    where intent classification lives."""

    def test_fires_on_filename_intent_and_zero_results(self):
        d = _MockDecision(intent="filename")
        self.assertTrue(
            filename_extend_should_fire("Where is my file?", d, [])
        )

    def test_fires_on_mixed_intent_and_zero_results(self):
        # Mixed intent treated as filename-eligible because the
        # LLM was uncertain — we still want proactive to help.
        d = _MockDecision(intent="mixed")
        self.assertTrue(filename_extend_should_fire("anything", d, []))

    def test_does_not_fire_when_intent_is_semantic(self):
        # LLM said semantic — trust it, do NOT enumerate phrases.
        d = _MockDecision(intent="semantic")
        self.assertFalse(filename_extend_should_fire("Where is my doc?", d, []))
        self.assertFalse(filename_extend_should_fire("找一下任务清单", d, []))
        self.assertFalse(filename_extend_should_fire("我的笔记在哪里", d, []))

    def test_does_not_fire_when_intent_is_lexical(self):
        d = _MockDecision(intent="lexical")
        self.assertFalse(
            filename_extend_should_fire("auth login", d, [])
        )

    def test_does_not_fire_with_none_decision(self):
        # Principle 1: when no understanding is available, refuse
        # rather than enumerate keyword phrases. The CLI always
        # passes a decision; non-CLI callers must too.
        self.assertFalse(filename_extend_should_fire("where is my doc?", None, []))

    def test_fires_when_results_lack_token_match(self):
        # 0.2.8 update: even with non-empty cascade results, if NONE
        # of them have the lookup token in their basename, fire so
        # the user gets a chance to discover the actual file.
        d = _MockDecision(intent="filename", primary_token="task-001")
        results = [
            {"path": "/some/dir/unrelated.md"},
            {"path": "/another/place/reference.txt"},
        ]
        self.assertTrue(
            filename_extend_should_fire("where is my task-001 file", d, results)
        )

    def test_does_not_fire_when_results_have_token_match(self):
        # When the cascade already surfaced the file, don't bother
        # re-running the proactive search.
        d = _MockDecision(intent="filename", primary_token="task-001")
        results = [{"path": "/somewhere/task-001-spec.md"}]
        self.assertFalse(
            filename_extend_should_fire("where is my task-001 file", d, results)
        )


class FilenameExtendExecuteTests(unittest.TestCase):
    """Use injectable ``search_dirs`` so we don't touch the user's
    real ``~/Downloads`` etc. during unit tests."""

    def setUp(self):
        if shutil.which("find") is None:
            self.skipTest("`find` not on PATH")
        self.tmp = tempfile.mkdtemp(prefix="skygrep_proactive_test_")
        # Lay down two findable hits and one decoy.
        self.hit1 = Path(self.tmp) / "task-001-spec.md"
        self.hit2 = Path(self.tmp) / "subdir" / "task-001-notes.txt"
        self.decoy = Path(self.tmp) / "unrelated.md"
        self.hit1.parent.mkdir(parents=True, exist_ok=True)
        self.hit2.parent.mkdir(parents=True, exist_ok=True)
        for p_ in (self.hit1, self.hit2, self.decoy):
            p_.write_text("x")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_finds_files_with_token_in_basename(self):
        d = _MockDecision(primary_token="task-001")
        result = filename_extend_execute(
            "where is my task-001 file?",
            d, top_k=10, individual_budget_ms=2000,
            search_dirs=[Path(self.tmp)],
        )
        self.assertIsNotNone(result)
        names = {Path(h["path"]).name for h in result.extra_hits}
        self.assertIn("task-001-spec.md", names)
        self.assertIn("task-001-notes.txt", names)
        self.assertNotIn("unrelated.md", names)

    def test_returns_none_with_unfindable_token(self):
        d = _MockDecision(primary_token="zzznotapresenthere")
        result = filename_extend_execute(
            "where is zzznotapresenthere?",
            d, top_k=10, individual_budget_ms=2000,
            search_dirs=[Path(self.tmp)],
        )
        self.assertIsNone(result)

    def test_returns_none_with_no_search_dirs(self):
        d = _MockDecision(primary_token="task-001")
        result = filename_extend_execute(
            "where is task-001?",
            d, top_k=10, individual_budget_ms=2000,
            search_dirs=[Path("/this/does/not/exist")],
        )
        self.assertIsNone(result)

    def test_extracts_token_from_query_when_primary_is_empty(self):
        """When the LLM didn't supply ``primary_token`` (rule-based
        fallback / older cache), the enhancer's own token-extractor
        picks the most identifier-like word. The fixture has files
        named ``task-001-*.md`` and the query mentions ``task-001``
        as the most identifier-like token among interrogatives."""

        d = _MockDecision(primary_token="")
        result = filename_extend_execute(
            "where is my task-001 file",
            d, top_k=10, individual_budget_ms=2000,
            search_dirs=[Path(self.tmp)],
        )
        self.assertIsNotNone(result)
        self.assertGreater(len(result.extra_hits), 0)


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


class RenderTests(unittest.TestCase):
    def test_empty_results_render_empty(self):
        self.assertEqual(render_proactive_output([]), "")

    def test_single_result_includes_note_and_paths(self):
        pr = ProactiveResult(
            enhancer_name="filename_extend",
            extra_hits=[{"path": "/tmp/foo.txt", "score": 0.0}],
            note="Found 1 match outside the project root:",
            commands=['cd /tmp && skygrep "foo"'],
        )
        rendered = render_proactive_output([pr])
        self.assertIn("Found 1 match", rendered)
        self.assertIn("/tmp/foo.txt", rendered)
        self.assertIn("→ next:", rendered)


# ---------------------------------------------------------------------------
# Built-in registration
# ---------------------------------------------------------------------------


class BuiltInRegistrationTests(unittest.TestCase):
    def test_filename_extend_is_registered_at_import(self):
        # The module-level ``register_enhancer`` call should have
        # added ``filename_extend`` to the registry on import.
        self.assertIn("filename_extend", list_enhancers())


if __name__ == "__main__":
    unittest.main()
