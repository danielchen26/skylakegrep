"""Proactive enhancement framework — Principle 6 in action.

The motivating case: a user issues a clear filename-lookup query
from one directory but the file actually lives under a sibling
home tree (``~/Downloads`` / ``~/Desktop`` / etc.). The
in-project ``filename_shortcut`` correctly returns "no match"
because it's bounded to ``project_root``, and the cold-start path
prints "No matches yet — semantic index is building". The user's
file IS findable via ``find`` against a different root, but the
old flow required the user to figure that out and re-issue the
query. The 0.2.4–0.2.6 ``intelligent_cli`` family fixed *passive*
shrugs (out-of-scope hints, low-confidence menus) — but the system
still just told the user "I couldn't find it" instead of going
looking.

This module is the architectural answer: a **content-agnostic
enhancer registry** that runs IN PARALLEL after the main cascade
returns, with a hard latency budget so normal queries pay zero
extra cost. Each enhancer has its own ``should_fire`` gate and
its own per-enhancer budget; only enhancers whose gate fires get
scheduled, and the ``ThreadPoolExecutor`` cancels anything that
overruns the total budget.

Design contract (must hold for every enhancer):
  1. **Should-fire is cheap.** O(1) on the inputs we already have
     (query string, ``RouterDecision``, ``results`` list). No
     I/O, no LLM calls inside ``should_fire``. We don't pay for
     enhancers that aren't going to fire.
  2. **Latency is bounded.** Each ``execute`` returns within its
     own ``individual_budget_ms`` or gets cancelled. The pool's
     ``total_budget_ms`` (``SKYGREP_PROACTIVE_BUDGET_MS``, default
     500 ms) is the wall clock cap across all enhancers combined.
  3. **Failure is silent.** Enhancers raise → we log debug and
     drop their result; the user still gets the main results.
  4. **Content-agnostic.** ``register_enhancer()`` is the
     extension point. Future enhancers (markdown link traversal,
     PDF section extraction, git-history-related, query
     refinement) plug in without touching this module.

This is the same architectural shape as
``reference_graph.register_extractor()`` (0.2.0) and the LLM
router prompt extension (0.2.6). Substrate before scaffolding;
understanding over enumeration; **proactive over passive**.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)


# Total proactive wall-clock budget. The 0.2.7-0.2.9 default of 500 ms
# was set against simple-shape `find` benchmarks but turned out to be
# unrealistic for actual home-directory walks at depth 4 — `find` on a
# few-hundred-file ``~/Downloads`` measured ~160 ms / dir, ``~/Documents``
# at ~600 ms / dir. With three dirs in parallel + thread-pool overhead +
# Python-level result handling, real wall-clock is ~700 ms. The 0.2.10
# default is 2000 ms, which keeps a comfortable margin for slower disks
# and busier home dirs without making the user perceptibly wait —
# `should_fire` still gates this so normal queries (cosine returned good
# results) pay zero cost.
DEFAULT_TOTAL_BUDGET_MS = 2000


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ProactiveContext:
    """Runtime state passed to enhancers that need access beyond
    the ``(query, decision, results)`` triple.

    Older enhancers (``filename_extend``) don't need any of this and
    accept ``ctx=None`` for forward-compat. Added in 0.2.11 so
    ``recovery_progress_hint`` can read live recovery state.

    Important: ``conn`` is the main-thread sqlite handle and
    **MUST NOT** be passed to enhancer worker threads (sqlite's
    ``check_same_thread`` would raise). The runner pre-fetches any
    state worker enhancers might need (``recovery_state``) on the
    main thread before submitting to the pool, so worker threads
    only see immutable snapshots — no cross-thread sqlite access.
    """

    conn: Any | None = None
    project_root: Any | None = None
    # Pre-fetched on the main thread by ``run_enhancers_parallel``
    # before the worker pool starts. Snapshot of recovery progress
    # at the moment the search command launched; enhancers read
    # this dict instead of the live ``conn`` to avoid sqlite's
    # cross-thread restrictions.
    recovery_state: dict | None = None


@dataclass
class ProactiveResult:
    """One enhancer's contribution to the user-visible output.

    ``extra_hits`` is rendered as additional result rows after the
    main cascade results. ``note`` is a single line that introduces
    the section so the user can see WHO produced these (not just
    that they appeared). ``commands`` is an optional list of
    "what would you run next" suggestions the CLI can echo.
    """

    enhancer_name: str
    extra_hits: list[dict] = field(default_factory=list)
    note: str = ""
    commands: list[str] = field(default_factory=list)


@dataclass
class ProactiveEnhancement:
    """One registered enhancer.

    ``should_fire`` and ``execute`` are the two hooks; ``name`` is
    surfaced in telemetry. The runner enforces
    ``individual_budget_ms`` per execute call regardless of what
    the enhancer body does internally.
    """

    name: str
    should_fire: Callable[[str, Any, list[dict]], bool]
    execute: Callable[[str, Any, int, int], Optional[ProactiveResult]]
    individual_budget_ms: int = 400


_REGISTRY: list[ProactiveEnhancement] = []


def register_enhancer(enh: ProactiveEnhancement) -> None:
    """Add ``enh`` to the global registry. Idempotent on
    ``enh.name`` so re-importing the module during tests doesn't
    duplicate built-ins."""

    for i, existing in enumerate(_REGISTRY):
        if existing.name == enh.name:
            _REGISTRY[i] = enh
            return
    _REGISTRY.append(enh)


def clear_registry() -> None:
    """Test-only: drop all registered enhancers. Production code
    should not touch this."""

    _REGISTRY.clear()


def list_enhancers() -> list[str]:
    """Return registered enhancer names. Used by tests + telemetry."""

    return [e.name for e in _REGISTRY]


# ---------------------------------------------------------------------------
# Budget enforcement / parallel runner
# ---------------------------------------------------------------------------


def _proactive_disabled() -> bool:
    """Master kill-switch shared with ``intelligent_cli.hints_disabled``
    semantics; ``SKYGREP_NO_PROACTIVE=1`` disables the whole framework
    independently of ``SKYGREP_NO_HINTS=1`` so users can keep
    intelligent-CLI hints but turn off the parallel-extra-work."""

    return (
        os.environ.get("SKYGREP_NO_PROACTIVE") == "1"
        or os.environ.get("SKYGREP_NO_HINTS") == "1"
    )


def _total_budget_ms() -> int:
    raw = os.environ.get("SKYGREP_PROACTIVE_BUDGET_MS")
    if raw is None:
        return DEFAULT_TOTAL_BUDGET_MS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_TOTAL_BUDGET_MS


def run_enhancers_parallel(
    query: str,
    decision: Any,
    results: list[dict],
    *,
    top_k: int,
    total_budget_ms: int | None = None,
    ctx: ProactiveContext | None = None,
) -> tuple[list[ProactiveResult], dict]:
    """Run all registered enhancers whose ``should_fire`` gate
    returns ``True`` in parallel. Return whatever finishes within
    the total budget; cancel the rest.

    Returns ``(results, telemetry)`` where ``telemetry`` is a dict
    suitable for inclusion in the cascade footer:

      * ``fired``: list of enhancer names that were scheduled
      * ``completed``: list of names whose ``execute`` returned
      * ``timed_out``: list of names cancelled by the budget
      * ``budget_ms``: the total budget that was enforced
      * ``elapsed_ms``: actual wall clock time spent in the pool

    The runner is fail-soft: any enhancer raising during
    ``should_fire`` is treated as "didn't fire", and any enhancer
    raising / returning ``None`` from ``execute`` contributes
    nothing to the result list.
    """

    if _proactive_disabled() or not _REGISTRY:
        return [], {
            "fired": [], "completed": [], "timed_out": [],
            "budget_ms": 0, "elapsed_ms": 0,
        }

    if ctx is None:
        ctx = ProactiveContext()

    # Pre-fetch recovery state on the main thread so worker threads
    # that need it (``recovery_progress_hint``) don't try to cross
    # sqlite's check_same_thread guard. Best-effort: enhancers fall
    # through to ``recovery_state=None`` if the read fails.
    if ctx.conn is not None and ctx.recovery_state is None:
        try:
            from .recovery import get_recovery_state
            ctx.recovery_state = get_recovery_state(ctx.conn)
        except Exception:
            logger.debug("failed to pre-fetch recovery state for proactive ctx", exc_info=True)
            ctx.recovery_state = None

    budget_ms = total_budget_ms if total_budget_ms is not None else _total_budget_ms()
    if budget_ms <= 0:
        return [], {
            "fired": [], "completed": [], "timed_out": [],
            "budget_ms": budget_ms, "elapsed_ms": 0,
        }

    # Should-fire gate first — never schedule an enhancer that
    # doesn't want to run. ``should_fire`` exceptions are silently
    # treated as "no" so a buggy gate can't break search.
    eligible: list[ProactiveEnhancement] = []
    for enh in _REGISTRY:
        try:
            if _call_with_optional_ctx(
                enh.should_fire, query, decision, results, ctx=ctx,
            ):
                eligible.append(enh)
        except Exception:
            logger.debug("enhancer %r should_fire raised; skipping", enh.name, exc_info=True)
    if not eligible:
        return [], {
            "fired": [], "completed": [], "timed_out": [],
            "budget_ms": budget_ms, "elapsed_ms": 0,
        }

    started = time.monotonic()
    deadline_s = started + (budget_ms / 1000.0)
    out: list[ProactiveResult] = []
    completed: list[str] = []
    timed_out: list[str] = []

    # Use try/finally + shutdown(wait=False, cancel_futures=True)
    # rather than the ``with`` context manager. The context manager's
    # ``__exit__`` blocks on every running thread, defeating the
    # whole point of the budget — a slow ``find`` /
    # ``subprocess.run`` would let the runner's wall-clock balloon
    # past the budget. With ``wait=False, cancel_futures=True``:
    #   - pending futures get cancelled outright
    #   - running futures keep running in the background, but our
    #     caller returns within the budget
    # Production enhancers MUST respect their own
    # ``individual_budget_ms`` internally (passed to
    # ``subprocess.run(timeout=...)``, etc.); this runner's budget
    # is the wall-clock cap on the user's perceived latency, not a
    # CPU-level cancellation.
    pool = ThreadPoolExecutor(max_workers=min(len(eligible), 6))
    try:
        future_to_enh = {
            pool.submit(
                _safe_execute, enh, query, decision, top_k, enh.individual_budget_ms,
                ctx,
            ): enh
            for enh in eligible
        }
        try:
            remaining = max(0.001, deadline_s - time.monotonic())
            for fut in as_completed(future_to_enh, timeout=remaining):
                enh = future_to_enh[fut]
                try:
                    result = fut.result(timeout=0.001)  # already done
                except Exception:
                    logger.debug("enhancer %r execute raised; dropping", enh.name, exc_info=True)
                    continue
                completed.append(enh.name)
                if result is not None:
                    out.append(result)
        except TimeoutError:
            pass
        # Anything still un-done at this point exceeded the budget.
        for fut, enh in future_to_enh.items():
            if enh.name not in completed and not fut.done():
                timed_out.append(enh.name)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    elapsed_ms = int((time.monotonic() - started) * 1000)
    return out, {
        "fired": [e.name for e in eligible],
        "completed": completed,
        "timed_out": timed_out,
        "budget_ms": budget_ms,
        "elapsed_ms": elapsed_ms,
    }


def _safe_execute(
    enh: ProactiveEnhancement,
    query: str,
    decision: Any,
    top_k: int,
    individual_budget_ms: int,
    ctx: ProactiveContext,
) -> Optional[ProactiveResult]:
    """Wrapper that enforces the individual budget at the wall
    clock level. The enhancer is responsible for honouring the
    budget internally (e.g. passing it as a subprocess timeout);
    this wrapper just guarantees that any leakage doesn't break
    the runner."""

    try:
        return _call_with_optional_ctx(
            enh.execute, query, decision, top_k, individual_budget_ms,
            ctx=ctx,
        )
    except Exception:
        logger.debug("enhancer %r raised inside execute", enh.name, exc_info=True)
        return None


def _call_with_optional_ctx(fn, *args, ctx: ProactiveContext):
    """Pass ``ctx`` to ``fn`` only if its signature accepts it.

    Older enhancers (``filename_extend``) were written before the
    0.2.11 ``ProactiveContext`` parameter existed. Their signatures
    don't include ``ctx``, so calling them with ``ctx=...`` would
    raise ``TypeError``. This shim inspects the function and only
    passes ``ctx`` when it's a recognised parameter — keeping the
    runner backward-compatible with all enhancers (built-in or
    third-party) regardless of when they were written.
    """

    import inspect

    try:
        sig = inspect.signature(fn)
        if "ctx" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in sig.parameters.values()
        ):
            return fn(*args, ctx=ctx)
    except (TypeError, ValueError):
        pass
    return fn(*args)


# ---------------------------------------------------------------------------
# Built-in enhancer: filename_extend (the req-2024-08 case)
# ---------------------------------------------------------------------------


# Default search roots when the current project_root has no hits.
# Order matters — common locations first so quick wins get
# scheduled before slower full-home walks.
def _default_search_dirs() -> list[Path]:
    home = Path.home()
    return [
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
    ]


# Token-extraction helpers reused from ``auto_index`` so the
# proactive enhancer scores tokens identically to the in-project
# filename_shortcut path. Importing lazily avoids a circular
# import: cli.py imports both modules, and auto_index imports
# storage which we may need too.


def _filename_token(query: str, decision: Any) -> str:
    """Pick the most filename-identifier-like token from the
    query, preferring a non-empty ``decision.primary_token`` when
    the LLM router has already done the work for us."""

    primary = getattr(decision, "primary_token", "") or ""
    if isinstance(primary, str) and len(primary.strip()) >= 2:
        return primary.strip()
    try:
        from .auto_index import _FN_TOKEN_RE, _FN_QUESTION_WORDS
    except Exception:
        return ""
    raw = _FN_TOKEN_RE.findall(query)
    candidates = [
        t for t in raw if t.lower() not in _FN_QUESTION_WORDS and len(t) >= 3
    ]
    if not candidates:
        return ""

    def score(t: str) -> int:
        s = 0
        if any(c.isdigit() for c in t):
            s += 100  # task-001 / v6.2 / req-2024-08-style identifiers
        if any(c in "._-" for c in t):
            s += 50
        if t != t.lower() and t != t.upper():
            s += 20
        return s

    best = sorted(candidates, key=lambda t: (-score(t), -len(t)))
    return best[0]


def _looks_like_identifier(token: str) -> bool:
    """Token-shape classifier (content morphology, NOT keyword
    enumeration): is this token specific enough to be worth
    filename-matching against?

    Three signals (any one suffices):
      - has digits  (``v6.2``, ``task-001``, ``inv-2024``)
      - has internal punctuation  (``foo.bar``, ``my-file``)
      - mixed case  (``CamelCase``, ``PascalCase``)

    Used in the gate's morphology-fallback path (when LLM didn't
    supply ``primary_token``) to decide whether the candidate token
    is identifier-quality. Without this filter, queries like
    ``"how does cascade work"`` would treat ``cascade`` as a
    filename candidate, surfacing false positives. With it, only
    structurally-distinctive tokens trigger filename-extend.

    The LLM router prompt uses the same family of signals to score
    candidate ``primary_token`` choices, so we share the criterion
    at the code level.
    """

    if not token or len(token) < 3:
        return False
    return (
        any(c.isdigit() for c in token)
        or any(c in "._-" for c in token)
        or (token != token.lower() and token != token.upper())
    )


def filename_extend_should_fire(
    query: str, decision: Any, results: list[dict],
) -> bool:
    """Fire when conventional retrieval can't answer the user's
    query under the current scope.

    Per Principle 6 (Proactive over Passive) **with no auxiliary
    gating**: the gate looks at exactly one signal — did the cascade
    return useful results? It does NOT inspect ``decision.intent``
    (that would re-introduce intent-shape gating which the user has
    explicitly rejected), it does NOT enumerate trigger phrases, it
    does NOT do token-shape morphology checks. Token-shape /
    morphology decisions belong INSIDE ``filename_extend_execute``,
    where the enhancer can early-return ``None`` without firing
    ``find`` if no usable token can be extracted — keeping the
    latency cost at exactly zero for queries where filename search
    would be useless.

    Two cases (the only two):

      - ``results`` empty → conventional retrieval failed; fire.
      - ``results`` non-empty → fire only if the LLM-provided
        ``primary_token`` does NOT appear in any result's basename
        (cascade returned semantically-related noise but not the
        actual file the user asked for). When the LLM didn't
        provide a token, trust that the cascade answered and don't
        fire — there's nothing to validate against.

    Why this is the right shape:

      - **No intent gating.** Whatever the LLM classified the
        intent as, if the cascade couldn't answer, the user is
        worse off than if we tried. Per Principle 6, we try.
      - **No keyword gating.** Trigger phrases (``where is`` / ``在哪``
        / ``find me`` / ``我的``) are gone — that was the 0.2.7 lapse
        and is not coming back.
      - **No token-shape gating at the gate.** The 0.2.9 attempt to
        gate on identifier morphology was correctly criticised by
        the user as the same anti-pattern wearing different
        clothes. Token-shape decisions live one layer deeper, in
        ``filename_extend_execute``, so they affect what the
        enhancer DOES, not whether it's eligible.
      - **Zero latency penalty in the common case.** Pure-NL queries
        with 0 results (``how does cascade work``) reach
        ``execute``, which calls ``_filename_token``, which returns
        an empty string because the regex finds no
        identifier-eligible candidate, which causes early ``return
        None``. ``find`` is never invoked, no subprocess started,
        no parallelism overhead beyond the thread-pool startup.
        Queries that DO have an identifier token pay the
        ``find`` cost — but those are the exact queries we want
        to extend.
    """

    if decision is None:
        return False
    if not results:
        return True
    # Validation against existing results: do any of them actually
    # match the user's lookup token in their basename? If yes, the
    # cascade answered. If no, the cascade returned noise (e.g. rg
    # cold-start matched the token as a substring inside unrelated
    # files like UUIDs in Julia ``Project.toml`` / ``Manifest.toml``)
    # — we should still extend the search.
    #
    # Token source priority: (1) LLM-supplied ``primary_token`` if
    # present (Principle 1: trust the LLM). (2) ``_filename_token``
    # morphology fallback when LLM was unreachable / didn't fill the
    # field. The 0.2.10 gate gave up at step (1) without (2), which
    # silenced proactive on every cold-start where the rule-based
    # router runs (no Ollama available, network down, etc.). This
    # is a content-shape fallback, not keyword enumeration —
    # ``_filename_token`` runs the same regex + identifier-shape
    # scoring the LLM router uses internally to score
    # ``primary_token`` candidates.
    primary_token = (getattr(decision, "primary_token", "") or "").strip()
    if not primary_token:
        candidate = (_filename_token(query, decision) or "").strip()
        # Only accept a morphology-extracted token if it looks like
        # an identifier (digit / punctuation / mixed case). Without
        # this check, queries like ``"how does cascade work"`` would
        # treat ``cascade`` as a filename candidate and fire on
        # any non-cascade result, surfacing irrelevant matches from
        # ``~/Downloads``. The LLM-supplied ``primary_token`` (when
        # present) is already vetted against the same shape signals
        # in the router prompt; the morphology check here aligns the
        # rule-based-fallback path with that contract.
        if candidate and _looks_like_identifier(candidate):
            primary_token = candidate
    if not primary_token:
        # Genuinely no usable identifier in the query (pure NL).
        # Trust the cascade.
        return False
    token_lower = primary_token.lower()
    return not any(
        token_lower in Path(r.get("path", "")).name.lower()
        for r in results
        if r.get("path")
    )


def filename_extend_execute(
    query: str,
    decision: Any,
    top_k: int,
    individual_budget_ms: int,
    *,
    search_dirs: list[Path] | None = None,
) -> Optional[ProactiveResult]:
    """Run ``find -iname '*token*'`` in parallel across common
    home directories. Returns up to 10 hits with directory
    attribution.

    ``search_dirs`` is injectable so tests can point at
    ``tempfile.TemporaryDirectory`` fixtures without touching the
    real ``~/Downloads``."""

    token = _filename_token(query, decision)
    if not token or len(token) < 2:
        return None

    if search_dirs is None:
        search_dirs = _default_search_dirs()
    dirs = [d for d in search_dirs if d.exists() and d.is_dir()]
    if not dirs:
        return None

    # Per-dir timeout. **Each dir runs in its own thread + subprocess
    # in parallel** (see the ``ThreadPoolExecutor`` below), so each
    # one should receive the FULL ``individual_budget_ms`` — not
    # ``budget / N``. The wall-clock cap on the whole enhancer is
    # already enforced by ``as_completed(timeout=...)`` below.
    #
    # The 0.2.7–0.2.9 versions divided by ``len(dirs)``, which on a
    # 400 ms budget across 3 dirs gave only 133 ms per dir. ``find``
    # in a busy ``~/Downloads`` (a few hundred files at depth 4)
    # measured ~160 ms — over the budget by a hair, so subprocess
    # was killed before yielding stdout, and the user got 0 hits
    # despite having matching files. This fix gives each dir the
    # full budget; parallelism keeps the total wall clock bounded.
    per_dir_s = max(0.2, individual_budget_ms / 1000.0)

    all_hits: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=min(len(dirs), 4)) as pool:
        fut_to_dir = {
            pool.submit(_find_one_dir, d, token, per_dir_s): d
            for d in dirs
        }
        for fut in as_completed(fut_to_dir, timeout=individual_budget_ms / 1000.0 + 0.2):
            try:
                hits = fut.result(timeout=0.001)
            except Exception:
                continue
            d = fut_to_dir[fut]
            for h in hits[:5]:
                all_hits.append((h, str(d)))

    if not all_hits:
        return None

    # De-duplicate by absolute path while preserving directory
    # attribution from the first occurrence.
    seen: set[str] = set()
    deduped: list[tuple[str, str]] = []
    for h, d in all_hits:
        if h not in seen:
            seen.add(h)
            deduped.append((h, d))
    deduped = deduped[:10]

    extra_hits = [
        {
            "path": h,
            "score": 0.0,
            "language": Path(h).suffix.lstrip(".") or "file",
            "search_dir": d,
            "source": "proactive:filename_extend",
        }
        for h, d in deduped
    ]
    if not extra_hits:
        return None

    first_dir = Path(extra_hits[0]["search_dir"])
    return ProactiveResult(
        enhancer_name="filename_extend",
        extra_hits=extra_hits,
        note=(
            f"Found {len(extra_hits)} match(es) outside the current "
            f"project root while the cascade was running:"
        ),
        commands=[f'cd {first_dir} && skygrep "{query}"'],
    )


def _find_one_dir(d: Path, token: str, timeout_s: float) -> list[str]:
    """Run a single ``find`` against one directory. Bounded by
    ``timeout_s``; returns ``[]`` on timeout / error so the runner
    can compose partial results from the lucky dirs."""

    cmd = [
        "find", str(d),
        "-maxdepth", "4",
        "-iname", f"*{token}*",
        "-not", "-path", "*/.*",
        "-not", "-path", "*/node_modules/*",
        "-not", "-path", "*/.venv/*",
        "-not", "-path", "*/__pycache__/*",
        "-not", "-name", "~$*",
        "-not", "-name", "*.swp",
        "-not", "-name", ".#*",
        "-not", "-name", "*~",
        "-type", "f",
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return []
    out: list[str] = []
    for line in r.stdout.splitlines():
        s = line.strip()
        if not s:
            continue
        # Token-in-basename check guards against fluke substring
        # matches deep in the path. Same heuristic as
        # ``auto_index.filename_shortcut``.
        if token.lower() in Path(s).name.lower():
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Built-in enhancer: recovery_progress_hint (semantic queries during recovery)
# ---------------------------------------------------------------------------


# Threshold below which a top-1 cosine score is considered "low
# confidence" — when the index is partially built, even legitimate
# matches can score in the 0.3-0.5 band because the semantically-
# closest chunks haven't been re-embedded yet. The 0.5 floor is
# chosen to match the rule used elsewhere in the cascade for
# rerank-vs-trust decisions.
_RECOVERY_HINT_TOP1_FLOOR = 0.5


def recovery_progress_should_fire(
    query: str, decision: Any, results: list[dict], ctx: ProactiveContext = None,
) -> bool:
    """Fire when the user asked a semantic / content query but the
    semantic index is still being re-embedded by the 0.2.2 recovery
    worker, AND the cascade either returned nothing or only
    low-confidence hits. Tells the user to re-run after the index
    finishes — content matches that are currently invisible
    (because their owning files haven't been re-embedded yet) will
    surface then.

    Reads ``ctx.recovery_state`` (pre-fetched by the runner on the
    main thread) rather than ``ctx.conn`` — sqlite forbids
    cross-thread connection sharing.
    """

    if decision is None or ctx is None or ctx.recovery_state is None:
        return False
    intent = getattr(decision, "intent", "")
    if intent != "semantic":
        return False
    if results:
        try:
            top1 = float(results[0].get("score", 0.0))
        except (TypeError, ValueError):
            top1 = 0.0
        if top1 >= _RECOVERY_HINT_TOP1_FLOOR:
            return False
    return bool(ctx.recovery_state.get("in_progress"))


def recovery_progress_execute(
    query: str, decision: Any, top_k: int, individual_budget_ms: int,
    ctx: ProactiveContext = None,
) -> Optional[ProactiveResult]:
    """Render a one-line note explaining the partial index state
    and pointing the user at the right next steps. Reads
    ``ctx.recovery_state`` (the snapshot pre-fetched by the
    runner)."""

    if ctx is None or ctx.recovery_state is None:
        return None
    state = ctx.recovery_state
    progress = state.get("progress") or "?/?"
    coverage = state.get("coverage_pct")
    eta_seconds = state.get("eta_seconds")

    eta_str = "?"
    if eta_seconds:
        m = int(eta_seconds) // 60
        s = int(eta_seconds) % 60
        eta_str = f"{m}m{s:02d}s" if m else f"{s}s"

    coverage_str = f"{coverage}%" if coverage is not None else "?"

    note = (
        f"Your query is content-based but the semantic index is still "
        f"being re-embedded ({progress} chunks · {coverage_str} coverage · "
        f"ETA ~{eta_str}). Re-run this query after the recovery worker "
        f"finishes — files whose chunks haven't been re-embedded yet are "
        f"currently invisible to cosine search."
    )

    return ProactiveResult(
        enhancer_name="recovery_progress_hint",
        extra_hits=[],
        note=note,
        commands=[
            "skygrep stats     # current chunks / coverage",
            "skygrep doctor    # health + recovery status",
        ],
    )


# Register the built-in filename-extend enhancer at import time so
# the CLI doesn't have to remember to do it. Tests can call
# ``clear_registry()`` to reset.
register_enhancer(
    ProactiveEnhancement(
        name="filename_extend",
        should_fire=filename_extend_should_fire,
        execute=filename_extend_execute,
        # 1500 ms covers the slowest measured ``find`` on a real home
        # directory at depth 4 (``~/Documents`` ≈ 600 ms in our
        # development laptop benchmarks), with comfortable headroom.
        # The 0.2.7–0.2.9 default was 400 ms which was too tight even
        # for ``~/Downloads`` (≈ 160 ms with a ``find`` of a few
        # hundred files), and the per-dir budget was further divided
        # by the number of dirs — a double bug fixed in 0.2.10.
        individual_budget_ms=1500,
    )
)

register_enhancer(
    ProactiveEnhancement(
        name="recovery_progress_hint",
        should_fire=recovery_progress_should_fire,
        execute=recovery_progress_execute,
        # The execute body is a single SQL select on the metadata
        # table; 100 ms is plenty.
        individual_budget_ms=100,
    )
)


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def render_proactive_output(results: list[ProactiveResult]) -> str:
    """Format proactive results as a footer-block string the CLI
    can echo after the main results. ``""`` when nothing fired."""

    if not results:
        return ""
    lines: list[str] = []
    for pr in results:
        lines.append("")
        lines.append(f"💡 {pr.note}")
        for hit in pr.extra_hits:
            sz_kb = ""
            try:
                sz = Path(hit["path"]).stat().st_size
                sz_kb = f" · {sz // 1024} KB" if sz else ""
            except Exception:
                pass
            lines.append(f"   📄 {hit['path']}{sz_kb}")
        for cmd in pr.commands:
            lines.append(f"   → next: {cmd}")
    return "\n".join(lines)
