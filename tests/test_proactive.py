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
    ProactiveContext,
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

    def test_filename_extend_respects_explicit_scope(self):
        decision = _MockDecision(intent="filename", primary_token="CASE42")
        ctx = ProactiveContext(explicit_scope=True)
        self.assertFalse(
            filename_extend_should_fire(
                "where is CASE42 file in project folder",
                decision,
                [],
                ctx=ctx,
            )
        )

    def test_filename_extend_can_use_plain_descriptor_token_when_intent_is_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "project-report.pdf"
            target.write_text("x")
            decision = _MockDecision(intent="filename", primary_token="")

            result = filename_extend_execute(
                "where is project report file",
                decision,
                top_k=5,
                individual_budget_ms=1000,
                search_dirs=[root],
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(Path(result.extra_hits[0]["path"]).name, "project-report.pdf")


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
    """The home-dir filename extender is scoped to filename intent.

    rg/semantic hits are allowed to provide fast feedback, but they
    must not trigger unrelated home-directory walks unless the router
    has understood the query as a filename lookup.
    """

    def test_fires_on_filename_intent_and_zero_results(self):
        d = _MockDecision(intent="filename")
        self.assertTrue(
            filename_extend_should_fire("Where is my file?", d, [])
        )

    def test_does_not_fire_on_mixed_intent_and_zero_results(self):
        d = _MockDecision(intent="mixed")
        self.assertFalse(filename_extend_should_fire("anything", d, []))

    def test_gate_fires_on_zero_results_only_for_filename_intent(self):
        for intent in ("semantic", "lexical", "mixed"):
            with self.subTest(intent=intent):
                d = _MockDecision(intent=intent)
                self.assertFalse(
                    filename_extend_should_fire("any query", d, []),
                    msg=f"non-filename intent must not launch home-dir find, intent={intent}",
                )
        self.assertTrue(
            filename_extend_should_fire(
                "where is my task-001 file",
                _MockDecision(intent="filename"),
                [],
            )
        )

    def test_does_not_fire_when_results_match_primary_token(self):
        # When LLM gave a primary_token AND the cascade already
        # surfaced a file whose basename contains it, don't fire.
        d = _MockDecision(intent="filename", primary_token="task-001")
        self.assertFalse(
            filename_extend_should_fire(
                "where is task-001", d,
                [{"path": "/dir/task-001-spec.md"}],
            )
        )

    def test_fires_when_results_present_but_no_primary_token_match(self):
        # Cascade returned non-empty results but none of them have
        # the primary_token in their basename → gate fires (cascade
        # surfaced semantically-related noise, not the file).
        d = _MockDecision(intent="filename", primary_token="task-001")
        results = [
            {"path": "/some/dir/unrelated.md"},
            {"path": "/another/place/reference.txt"},
        ]
        self.assertTrue(
            filename_extend_should_fire("where is task-001", d, results)
        )

    def test_does_not_fire_when_results_present_and_no_primary_token(self):
        # 0.2.12 update: when LLM didn't provide primary_token AND
        # the query has no identifier-shape token at all (pure NL),
        # the morphology-fallback returns "" too — trust the cascade.
        d = _MockDecision(intent="semantic", primary_token="")
        self.assertFalse(
            filename_extend_should_fire(
                "how does cascade work", d,
                [{"path": "/some/file.py"}],
            )
        )

    def test_fires_on_filename_noise_with_empty_primary_token(self):
        """When the router classifies filename intent but lacks a
        primary token, morphology fallback still recognises the lookup
        token and validates that current results missed the file."""
        d = _MockDecision(intent="filename", primary_token="")
        rg_noise_results = [
            {"path": "/proj/LotkaVolterra/Project.toml", "score": 1.0},
            {"path": "/proj/LotkaVolterra/Manifest.toml", "score": 1.0},
        ]
        self.assertTrue(
            filename_extend_should_fire(
                '我有没有跟"case42"有关的文件？', d, rg_noise_results,
            )
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
        first = result.extra_hits[0]
        self.assertEqual(first["fallback"], "filename-lookup")
        self.assertEqual(first["file"], first["path"])
        self.assertIn("size:", first["snippet"])
        self.assertIn("modified:", first["snippet"])

    def test_returns_after_first_directory_hit_without_waiting_for_slow_sibling(self):
        fast_dir = Path(self.tmp) / "fast"
        slow_dir = Path(self.tmp) / "slow"
        fast_dir.mkdir()
        slow_dir.mkdir()
        hit = fast_dir / "task-001-fast.md"
        hit.write_text("x", encoding="utf-8")

        def fake_find_one_dir(d: Path, token: str, timeout_s: float):
            if d == fast_dir:
                return [str(hit)]
            time.sleep(1.2)
            return []

        d = _MockDecision(primary_token="task-001")
        start = time.monotonic()
        with patch.object(p, "_find_one_dir", side_effect=fake_find_one_dir):
            result = filename_extend_execute(
                "where is task-001?",
                d,
                top_k=10,
                individual_budget_ms=2000,
                search_dirs=[fast_dir, slow_dir],
            )
        elapsed = time.monotonic() - start

        self.assertIsNotNone(result)
        self.assertLess(elapsed, 0.75)
        self.assertEqual(
            {Path(h["path"]).name for h in result.extra_hits},
            {"task-001-fast.md"},
        )

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

    def test_extracts_identifier_from_multilingual_primary_token(self):
        d = _MockDecision(intent="filename", primary_token="我的 task-001 文件")
        result = filename_extend_execute(
            "我的 task-001 文件在哪",
            d, top_k=10, individual_budget_ms=2000,
            search_dirs=[Path(self.tmp)],
        )
        self.assertIsNotNone(result)
        names = {Path(h["path"]).name for h in result.extra_hits}
        self.assertIn("task-001-spec.md", names)


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
        self.assertIn("next:", rendered)

    def test_full_detail_renders_filename_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            hit_path = Path(temp_dir) / "CASE42_Project_Report.txt"
            hit_path.write_text("generic content marker\n", encoding="utf-8")
            pr = filename_extend_execute(
                "where is CASE42 file",
                _MockDecision(intent="filename", primary_token="CASE42"),
                top_k=10,
                individual_budget_ms=2000,
                search_dirs=[Path(temp_dir)],
            )
            self.assertIsNotNone(pr)
            rendered = render_proactive_output(
                [pr],
                project_root=temp_dir,
                detail="full",
                content=True,
            )
        self.assertIn("CASE42_Project_Report.txt", rendered)
        self.assertIn("generic content marker", rendered)


# ---------------------------------------------------------------------------
# Built-in registration
# ---------------------------------------------------------------------------


class BuiltInRegistrationTests(unittest.TestCase):
    def test_filename_extend_is_registered_at_import(self):
        # The module-level ``register_enhancer`` call should have
        # added ``filename_extend`` to the registry on import.
        self.assertIn("filename_extend", list_enhancers())

    def test_recovery_progress_hint_is_registered_at_import(self):
        # 0.2.11: the recovery_progress_hint enhancer must auto-
        # register so cold-start content queries against an
        # in-progress index get the user-visible "still building"
        # notice for free.
        self.assertIn("recovery_progress_hint", list_enhancers())


class RecoveryProgressHintTests(unittest.TestCase):
    """0.2.11 enhancer regression coverage. Builds a real DB with
    recovery state set + a fake LLM-router decision and verifies
    the should_fire / execute / output triple end-to-end."""

    def _conn_with_recovery(self, in_progress=True, progress="100/200",
                            coverage_pct=50, eta_seconds=600):
        from skylakegrep.src.storage import init_db, set_meta
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        path = Path(tmp.name)
        conn = init_db(path)
        set_meta(conn, "recovery_in_progress", "1" if in_progress else "0")
        set_meta(conn, "recovery_progress", progress)
        set_meta(conn, "recovery_coverage_pct", str(coverage_pct))
        set_meta(conn, "recovery_eta_seconds", str(eta_seconds))
        # Fresh heartbeat so get_recovery_state doesn't mark as crashed.
        import time
        from skylakegrep.src.storage import set_meta as _sm
        _sm(conn, "recovery_heartbeat_at", str(time.time()))
        return conn, path

    def _ctx_for_recovery(self, conn, *, in_progress_state=None):
        """Build a ``ProactiveContext`` with ``recovery_state`` already
        populated, simulating what ``run_enhancers_parallel`` does on
        the main thread before scheduling enhancers."""
        from skylakegrep.src.proactive import ProactiveContext
        from skylakegrep.src.recovery import get_recovery_state
        state = (
            in_progress_state if in_progress_state is not None
            else get_recovery_state(conn)
        )
        return ProactiveContext(conn=conn, recovery_state=state)

    def test_fires_on_semantic_query_with_zero_results_during_recovery(self):
        from skylakegrep.src.proactive import (
            ProactiveContext, recovery_progress_should_fire,
        )
        conn, path = self._conn_with_recovery()
        try:
            d = _MockDecision(intent="semantic")
            ctx = self._ctx_for_recovery(conn)
            self.assertTrue(
                recovery_progress_should_fire(
                    "what does this paper say about quantum tunneling",
                    d, [], ctx=ctx,
                )
            )
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_does_not_fire_when_recovery_not_in_progress(self):
        from skylakegrep.src.proactive import (
            ProactiveContext, recovery_progress_should_fire,
        )
        conn, path = self._conn_with_recovery(in_progress=False)
        try:
            d = _MockDecision(intent="semantic")
            ctx = self._ctx_for_recovery(conn)
            self.assertFalse(
                recovery_progress_should_fire("any", d, [], ctx=ctx)
            )
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_does_not_fire_on_filename_intent(self):
        # filename_extend handles the filename case; recovery
        # progress is content-search-specific.
        from skylakegrep.src.proactive import (
            ProactiveContext, recovery_progress_should_fire,
        )
        conn, path = self._conn_with_recovery()
        try:
            d = _MockDecision(intent="filename")
            ctx = self._ctx_for_recovery(conn)
            self.assertFalse(
                recovery_progress_should_fire(
                    "where is my file", d, [], ctx=ctx,
                )
            )
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_does_not_fire_when_top1_score_is_high(self):
        # Cascade returned a confident hit even mid-recovery; no
        # need to nag the user with the partial-index notice.
        from skylakegrep.src.proactive import (
            ProactiveContext, recovery_progress_should_fire,
        )
        conn, path = self._conn_with_recovery()
        try:
            d = _MockDecision(intent="semantic")
            ctx = self._ctx_for_recovery(conn)
            self.assertFalse(
                recovery_progress_should_fire(
                    "any", d,
                    [{"path": "/a/b.py", "score": 0.85}],
                    ctx=ctx,
                )
            )
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_does_not_fire_with_no_ctx_or_no_recovery_state(self):
        from skylakegrep.src.proactive import (
            ProactiveContext, recovery_progress_should_fire,
        )
        d = _MockDecision(intent="semantic")
        self.assertFalse(recovery_progress_should_fire("q", d, [], ctx=None))
        self.assertFalse(
            recovery_progress_should_fire(
                "q", d, [], ctx=ProactiveContext(),
            )
        )

    def test_execute_renders_progress_and_eta(self):
        from skylakegrep.src.proactive import (
            ProactiveContext, recovery_progress_execute,
        )
        conn, path = self._conn_with_recovery(
            progress="1234/5000", coverage_pct=24, eta_seconds=754,
        )
        try:
            d = _MockDecision(intent="semantic")
            ctx = self._ctx_for_recovery(conn)
            res = recovery_progress_execute("q", d, 10, 100, ctx=ctx)
            self.assertIsNotNone(res)
            self.assertEqual(res.enhancer_name, "recovery_progress_hint")
            self.assertIn("1234/5000", res.note)
            self.assertIn("24%", res.note)
            self.assertIn("12m34s", res.note)
            # Commands list points the user at next steps.
            self.assertTrue(
                any("skygrep stats" in c for c in res.commands),
            )
        finally:
            conn.close()
            path.unlink(missing_ok=True)

    def test_end_to_end_via_run_enhancers_parallel(self):
        """The full production code path: register_enhancer
        already happened at import; we only need to set up the
        recovery state + decision + ctx and run the parallel
        runner. Verifies the wired-up `recovery_progress_hint`
        actually surfaces in the live registry."""
        from skylakegrep.src.proactive import (
            ProactiveContext, run_enhancers_parallel,
        )
        conn, path = self._conn_with_recovery(
            progress="2000/8000", coverage_pct=25, eta_seconds=300,
        )
        try:
            d = _MockDecision(intent="semantic")
            ctx = self._ctx_for_recovery(conn)
            results, telemetry = run_enhancers_parallel(
                "what does the paper say", d, [], top_k=10, ctx=ctx,
                total_budget_ms=2000,
            )
            names = [r.enhancer_name for r in results]
            self.assertIn("recovery_progress_hint", names)
            self.assertIn("recovery_progress_hint", telemetry["completed"])
        finally:
            conn.close()
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
