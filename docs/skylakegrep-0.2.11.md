# skylakegrep 0.2.11 — release notes

`0.2.11` adds the **second built-in proactive enhancer**:
`recovery_progress_hint`. Closes the gap the user identified after
0.2.10 — `filename_extend` covers the "where is my file?" case
but content-search queries against an in-progress index were
still getting the bare "No matches yet" cold-start message with
no useful next step. The new enhancer surfaces live recovery
progress + ETA so the user knows *why* they're not getting
results and *when* to retry.

Also includes a small infrastructure change (`ProactiveContext`)
that future enhancers will build on, plus a detailed plan filed
under `docs/plans/` for the next-layer "graph-prior folder
inference" work the user articulated during review.

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact the maintainers.

## What changed

### 1. New built-in enhancer: `recovery_progress_hint`

```
$ skygrep "what does the paper say about regularisation"
[main results — possibly 0 because the index is partially built]

💡 Your query is content-based but the semantic index is still
   being re-embedded (1234/35131 chunks · 23% coverage · ETA ~14m10s).
   Re-run this query after the recovery worker finishes — files whose
   chunks haven't been re-embedded yet are currently invisible to
   cosine search.
   → next: skygrep stats     # current chunks / coverage
   → next: skygrep doctor    # health + recovery status
```

**Should-fire conditions** (all required):

  - `decision.intent == "semantic"` — content query (not filename
    or lexical, those are covered by other paths)
  - `results` empty OR top-1 score < 0.5 — cascade didn't already
    answer well
  - Recovery worker is running — `recovery_state["in_progress"]`
    is True (read from the metadata table)

The gate doesn't fire on filename queries (those route to
`filename_extend` which already extends the search), on already-
confident results (no need to nag), or on indexes with no recovery
in progress (nothing to wait for).

**Cost:** zero added latency on the common case (the gate is O(1)
on a pre-fetched `recovery_state` dict — same shape as the
0.2.10 `should_fire` discipline). When it does fire, `execute`
just renders a string from the cached state — no I/O.

### 2. `ProactiveContext` — runtime state for enhancers

The enhancer signature has been extended to optionally accept a
`ProactiveContext` keyword arg:

```python
@dataclass
class ProactiveContext:
    conn: Any | None = None        # main-thread sqlite handle (NOT for workers)
    project_root: Any | None = None
    recovery_state: dict | None = None  # pre-fetched on main thread
```

The runner pre-fetches `recovery_state` from `conn` **on the main
thread** before submitting enhancers to the worker pool. Worker
threads then only see the snapshot — no cross-thread sqlite
access (which would otherwise raise `ProgrammingError` because of
sqlite's `check_same_thread` guard).

A signature-inspection shim (`_call_with_optional_ctx`) lets old
enhancers (`filename_extend`, registered before 0.2.11) keep
working unchanged: their `should_fire(query, decision, results)`
signature is detected and called without `ctx`. New enhancers can
opt in with `ctx=None` parameter or `**kwargs`. Backward-
compatible by construction.

### 3. CLI integration

Both the main cascade path and the cold-start path in
`cli.search_cmd` now construct a `ProactiveContext` with the live
SQLite handle and pass it to `run_enhancers_parallel`. The runner
pre-fetches recovery state once per query so enhancers can read
it cheaply.

### 4. `docs/plans/2026-05-05-graph-prior-folder-inference.md`

Per the user's instruction ("所有的我们的plan应该都写详细的写到
这个folder下面都要记录下来"), the bigger graph-prior architecture
they articulated during 0.2.11 review is filed as an open plan
document. Five signal sources, three implementation phases (G-1,
G-2, G-3), measurement plan as prerequisite, open questions
captured. Not on any release schedule yet — the 0.2.11
infrastructure (`ProactiveContext`) will fit naturally when Phase
G-1 lands.

## End-to-end verification (logged before tagging)

```
$ python -c "from skylakegrep.src.proactive import run_enhancers_parallel; ..."

telemetry: {'fired': ['filename_extend', 'recovery_progress_hint'],
            'completed': ['recovery_progress_hint', 'filename_extend'],
            'timed_out': [],
            'budget_ms': 2000,
            'elapsed_ms': 1170}

💡 Your query is content-based but the semantic index is still being
   re-embedded (1234/35131 chunks · 23% coverage · ETA ~14m10s).
   Re-run this query after the recovery worker finishes — files
   whose chunks haven't been re-embedded yet are currently invisible
   to cosine search.
   → next: skygrep stats     # current chunks / coverage
   → next: skygrep doctor    # health + recovery status
```

This is the production code path (`run_enhancers_parallel`)
fed exactly the inputs the cli would feed it: a recovery-in-progress
metadata DB, a semantic-intent decision, an empty results list.
Both registered enhancers fired, `recovery_progress_hint`
completed and rendered the user-visible note within 1170 ms total
budget (default 2000 ms cap).

## Test coverage

7 new tests in `tests/test_proactive.py::RecoveryProgressHintTests`:

  - `test_fires_on_semantic_query_with_zero_results_during_recovery`
  - `test_does_not_fire_when_recovery_not_in_progress`
  - `test_does_not_fire_on_filename_intent`
  - `test_does_not_fire_when_top1_score_is_high`
  - `test_does_not_fire_with_no_ctx_or_no_recovery_state`
  - `test_execute_renders_progress_and_eta`
  - `test_end_to_end_via_run_enhancers_parallel`

Plus `test_recovery_progress_hint_is_registered_at_import` in the
built-in registration suite.

Suite total: **200 / 200 passing** (20 subtests). Up from 192 in
0.2.10.

## Implementation files

  - `skylakegrep/src/proactive.py` — `ProactiveContext` dataclass,
    `_call_with_optional_ctx` shim, runner pre-fetch hook,
    `recovery_progress_should_fire`, `recovery_progress_execute`,
    enhancer registration.
  - `skylakegrep/src/cli.py` — both proactive call sites
    (cold-start + main cascade) updated to construct and pass
    `ProactiveContext(conn=conn, project_root=project_root)`.
  - `tests/test_proactive.py` — `RecoveryProgressHintTests` class
    with seven tests + builder helpers.
  - `docs/plans/2026-05-05-graph-prior-folder-inference.md` — the
    architecture plan document for the next-layer work.
  - `docs/PRINCIPLES.md` — Principle 6 receipts table updated
    (recovery_progress_hint is the second shipped enhancer; rows
    moved from "open" to "shipped").

## Compatibility

  - Python ≥ 3.9 (unchanged)
  - Existing 0.2.0–0.2.10 indexes: no migration; the
    `metadata` table keys read by `recovery_progress_hint` are
    set by the recovery worker that's been around since 0.2.2.
  - Bench numbers unchanged.

## Known follow-ups (not in 0.2.11)

  - **Phase G** — graph-prior folder inference; designed in
    [`docs/plans/2026-05-05-graph-prior-folder-inference.md`](plans/2026-05-05-graph-prior-folder-inference.md).
    Replaces hard-coded `~/Downloads`/`~/Desktop`/`~/Documents`
    with a content-agnostic, history-aware folder ranker.
  - **Phase C** — full intelligent-retrieval audit; tracked in
    [`docs/plans/2026-05-05-phase-c-audit.md`](plans/2026-05-05-phase-c-audit.md)
    + [`docs/plans/2026-05-05-phase-c-exploration.md`](plans/2026-05-05-phase-c-exploration.md).
  - More proactive enhancers (`query_refinement`,
    `markdown_link_traverse`, `pdf_section_extract`,
    `git_history_related`).
  - Re-render `docs/assets/{benchmark,schema,hero-dark,og-image}.svg`
    to reflect bge-m3 defaults.
  - Re-run the self-test bench on bge-m3 and update
    `docs/token-benchmarking.md`.
  - Fix the GitHub Actions `PYPI_API_TOKEN` 403; manual `twine`
    flow continues to work.
