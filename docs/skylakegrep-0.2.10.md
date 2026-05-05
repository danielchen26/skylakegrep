# skylakegrep 0.2.10 — release notes

`0.2.10` is the third bug-fix release in 24 hours for the proactive
framework — and the first one that's been **end-to-end verified
against the user's actual scenario before being shipped**. The
prior two releases (0.2.7 → 0.2.9) chased symptoms by tightening
gate logic; this one fixes the underlying mechanism.

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact <chentianchi@gmail.com>.

## What was wrong (root-cause diagnosis)

The user reported: `skygrep '我有没有跟"eb1b"有关的文件？'` from
`~/Documents` returned "No matches yet" even though the EB1B files
exist in `~/Downloads`. Across 0.2.7 / 0.2.8 / 0.2.9 the visible
behaviour didn't change — the gate fired correctly (we proved that
in unit tests), but the user still got no proactive output.

End-to-end timing measurements (the missing piece):

| | per-dir budget | actual `find` time | result |
| --- | :-: | :-: | :-: |
| `~/Downloads` | 133 ms | **161 ms** | ✗ killed before yielding stdout, 0 hits |
| `~/Desktop` | 133 ms | 79 ms | ✓ 0 hits (nothing matching) |
| `~/Documents` | 133 ms | 142 ms | ✗ killed before yielding stdout, 0 hits |

`find` was getting cut off by the per-dir timeout literally
milliseconds before it could return its results. The gate fired,
the enhancer executed, the subprocesses started, and they all got
SIGKILLed seconds before they would have surfaced the answer.

## Root causes (two of them, both shipped together as 0.2.10)

### Bug 1 — per-dir timeout was `total_budget / N` instead of `total_budget`

In `filename_extend_execute`:

```python
per_dir_s = max(0.1, (individual_budget_ms / 1000.0) / max(len(dirs), 1))
                                              ^^^^^^^^^^^^^^^^^^^^^^^^^
# Wrong: dirs run in parallel, NOT serially. Each thread should
# get the FULL budget; the wall-clock cap is enforced separately
# by the as_completed timeout below.
```

With 3 dirs and a 400 ms budget that became `400 / 3 = 133 ms` per
dir — under the actual `find` time. Each dir's `find` was killed
before yielding output. The same bug across 0.2.7 / 0.2.8 / 0.2.9.

**Fix:** `per_dir_s = max(0.2, individual_budget_ms / 1000.0)`. Each
parallel thread gets the full budget; total wall clock is bounded
by the `as_completed` timeout (`individual_budget_ms / 1000 + 0.2`).

### Bug 2 — defaults too tight for real home directories

The 0.2.7 defaults (`DEFAULT_TOTAL_BUDGET_MS = 500`, filename_extend
`individual_budget_ms = 400`) were set against simple-shape `find`
benchmarks but turned out to be unrealistic for actual depth-4 home
directory walks. Even after fixing bug 1, a 400 ms per-dir budget
was a hair under typical `~/Downloads` speed.

**Fix:** bumped defaults to `DEFAULT_TOTAL_BUDGET_MS = 2000`,
filename_extend `individual_budget_ms = 1500`. End-to-end measured
wall clock with 0.2.10 defaults: **1093 ms** to surface 4 EB1B files
across `~/Downloads / Desktop / Documents`. The user opted into the
proactive search by issuing a query the cascade couldn't answer; 1
second of parallel `find` with the right answer is much better than
500 ms of "no matches yet" with the wrong answer.

The should-fire gate continues to protect the common case — normal
queries with cosine top-1 confident DON'T fire, so latency stays at
zero for the 95 % path.

## Gate simplification (also part of 0.2.10)

Independent of the bug fix, 0.2.10 also simplifies the should-fire
gate to drop intent / token-shape filtering — a separate piece of
user feedback during 0.2.9 review:

> "你现在不是有一个intent吗任何的intent如果当前的问题识别不了或者
> 在当前的问题下识别不了应该触发这个intelligent proactive的东西"

The gate now does exactly one thing: check whether conventional
retrieval can answer. It does NOT inspect `decision.intent`, does
NOT call `_looks_like_identifier` (the 0.2.9 token-morphology
helper), does NOT enumerate trigger phrases (the 0.2.7 lapse).

```python
def filename_extend_should_fire(query, decision, results):
    if decision is None:
        return False
    if not results:
        return True   # cascade failed, fire unconditionally
    primary_token = decision.primary_token or ""
    if not primary_token:
        return False  # nothing to validate, trust the cascade
    return not any(
        primary_token.lower() in Path(r["path"]).name.lower()
        for r in results if r.get("path")
    )
```

Token-shape decisions live one layer deeper, **inside**
`filename_extend_execute`: the enhancer extracts a candidate token
via `_filename_token` (which uses the same morphology scoring the
LLM router uses to select `primary_token`), and returns `None` if
no usable token exists — keeping latency at exactly zero for queries
where filename search would be useless (pure NL with no identifier).

This is the cleanest split the user articulated:

  - **Gate (policy):** "did current scope fail?" — pure `results`
    check, no auxiliary signals.
  - **Enhancer (mechanism):** "given a query, can we answer it?" —
    silent return if no, parallel work if yes.

`docs/PRINCIPLES.md` Principle 1's receipts table now has FIVE
rows tracking the long iteration to get this right.

## Implementation files

  - `skylakegrep/src/proactive.py`:
    - `_looks_like_identifier` removed (dead code after gate
      simplification; token-shape checks live in `_filename_token`
      which already exists).
    - `filename_extend_should_fire` simplified — purely
      results-based.
    - `filename_extend_execute` — `per_dir_s = max(0.2,
      individual_budget_ms / 1000.0)` (no longer divides by N).
    - `DEFAULT_TOTAL_BUDGET_MS = 2000` (was 500).
    - `filename_extend.individual_budget_ms = 1500` (was 400).
  - `tests/test_proactive.py` — 4 tests rewritten / added to lock
    in the simpler gate (`test_gate_fires_on_zero_results_regardless_of_intent`,
    `test_does_not_fire_when_results_match_primary_token`,
    `test_fires_when_results_present_but_no_primary_token_match`,
    `test_does_not_fire_when_results_present_and_no_primary_token`).

## End-to-end verification (the part 0.2.7 / 0.2.8 / 0.2.9 missed)

Before tagging 0.2.10, the framework was tested against the user's
actual home directories with the user's exact query, using the
production `run_enhancers_parallel(...)` entry point and the new
defaults:

```
registered enhancers: ['filename_extend']
wall clock: 1093 ms
telemetry: {'fired': ['filename_extend'], 'completed': ['filename_extend'],
            'timed_out': [], 'budget_ms': 2000, 'elapsed_ms': 1093}

💡 Found 4 match(es) outside the current project root...
   /Users/.../Downloads/EB1B_Denial_Analysis.pdf
   /Users/.../Downloads/EB1B_Denial_Analysis.docx
   /Users/.../Downloads/My previous EB1B filling package pages 86-160.pdf
   /Users/.../Downloads/My previous EB1B filling package.pdf
```

This is what the user should see when they run their query against
`~/Documents` after upgrading. The cli code path (`cli.py`) calls
`run_enhancers_parallel(...)` from both the main cascade path
(0.2.7) and the cold-start path (0.2.8 fix), so this behaviour is
visible whether or not the index is built.

## Compatibility

  - Python ≥ 3.9 (unchanged)
  - Existing 0.2.0–0.2.9 indexes: no migration.
  - Bench numbers unchanged.
  - Test suite: **192 / 192 passing** (20 subtests).

## Lessons (recorded in PRINCIPLES.md and auto-memory)

1. **End-to-end test before declaring "shipped"** — 0.2.7, 0.2.8,
   and 0.2.9 each had passing unit tests for `filename_extend`
   but none had an end-to-end run against the user's actual
   home dirs with the production budgets. The user (correctly)
   pointed out that "测试了没有问题才能告诉我" — test it
   yourself, don't make the user be the integration test.

2. **Gate logic and mechanism logic are different concerns** —
   the gate is "should we even try" (results-based); the
   mechanism is "what do we do when we try" (token / regex /
   subprocess). Mixing them creates the keyword-enumeration
   anti-pattern the user has flagged 4× now.

3. **Latency budgets must reflect measured production** —
   400 ms / 500 ms looked good in benchmarks but were under the
   actual `find` time on a real `~/Downloads`. Always measure
   against real data before committing a budget number.

## Known follow-ups (not in 0.2.10)

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
