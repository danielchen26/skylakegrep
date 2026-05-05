# skylakegrep 0.2.7 — release notes

`0.2.7` ships **Principle 6 — Proactive over Passive** and its
first concrete enhancer (`filename_extend`). When the system can't
answer the user's query under its current bounded scope, it no
longer just shrugs ("no matches", "index is building"). It tries
extra work in parallel within a strict latency budget and
surfaces help the user can act on.

Also in this release: a sweep of user-personal examples
(`<token>` / <token> references) out of source code per the user's
explicit directive — replaced with generic identifier-shape
tokens (`task-001`, `req-2024-08`).

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact <chentianchi@gmail.com>.

## What changed

### 1. New `skylakegrep.src.proactive` framework

Content-agnostic enhancer registry, runs in parallel after the
main cascade returns, with a hard total-budget cap so normal
queries pay zero extra latency.

```python
from skylakegrep.src.proactive import (
    register_enhancer,
    ProactiveEnhancement,
    ProactiveResult,
)

def my_enhancer_should_fire(query, decision, results):
    # O(1), cheap. No I/O. No LLM calls.
    return decision.intent == "filename" and not results

def my_enhancer_execute(query, decision, top_k, individual_budget_ms):
    # Honour the budget internally (subprocess timeout, LLM timeout, …).
    return ProactiveResult(
        enhancer_name="my_enhancer",
        extra_hits=[...],
        note="Found additional matches:",
    )

register_enhancer(ProactiveEnhancement(
    name="my_enhancer",
    should_fire=my_enhancer_should_fire,
    execute=my_enhancer_execute,
    individual_budget_ms=400,
))
```

The runner uses `ThreadPoolExecutor.shutdown(wait=False,
cancel_futures=True)` so over-budget enhancers don't bleed into
the user's perceived latency. Every enhancer is failure-isolated
— one raising / hanging won't break the others. Master kill-switch
via `SKYGREP_NO_PROACTIVE=1` (and `SKYGREP_NO_HINTS=1`).

### 2. Built-in `filename_extend` enhancer

The motivating case: a user asks `where is my <file>?` from one
directory but the file actually lives in `~/Downloads` /
`~/Desktop` / `~/Documents`. The in-project filename shortcut
returns 0 hits because it's bounded to `project_root`. Before
0.2.7, the cold-start path printed `No matches yet — semantic
index is building`. Now:

```
$ skygrep "Where is my <file>?"
[main results render — 0 in this case]
[footer telemetry]

💡 Found 2 match(es) outside the current project root while the cascade was running:
   📄 /Users/.../Downloads/<file>.pdf · 2300 KB
   📄 /Users/.../Desktop/<file>-draft.docx · 145 KB
   → next: cd /Users/.../Downloads && skygrep "<query>"
```

Implementation:

  - Should-fire: `intent in {"filename", "mixed"}` (LLM router) OR
    English / Chinese natural-language lookup phrases (`where is`,
    `find me`, `locate`, `在哪`, `找一下`, ...) AND the cascade
    returned 0 results.
  - Execute: parallel `find -iname '*<token>*' -maxdepth 4` across
    `~/Downloads`, `~/Desktop`, `~/Documents` with a 400 ms
    individual budget. Reuses the `auto_index._FN_TOKEN_RE` /
    `_is_identifier_like` token-extractor from the in-project
    filename_shortcut path so scoring is consistent.
  - Search dirs are dependency-injectable (`search_dirs=...`) so
    tests target `tempfile.TemporaryDirectory` fixtures and never
    touch the user's real `~/Downloads`.

### 3. New `docs/PRINCIPLES.md` Principle 6

Full contract documented as Principle 6. Bounds (latency, gating,
content-agnosticism, failure-isolation, killable), what proactive
should NOT do (mutate state, replace the user, latency creep on
the common case), receipts table for shipped + open enhancers,
and a PR-checklist for new enhancers (state should-fire signal,
state individual_budget_ms, confirm no state mutation, contract
test).

### 4. Sweep of user-personal examples

Per the user's directive ("why you wrote <token> file in the actual
code that is just an example do not expose such thing"):

  - `skylakegrep/src/auto_index.py` — 5 in-comment references
    replaced with generic `task-001`-style examples.
  - `skylakegrep/src/binary_extract.py` — docstring example.
  - `skylakegrep/src/llm_router.py` — LLM router prompt example
    query (`"where is <token> file?"` → `"where is task-001 file?"`).
    Prompt still has 5 examples covering filename / semantic /
    lexical / recency-out-of-scope / size-out-of-scope intents.
  - `skylakegrep/src/proactive.py` — newly written, all
    pre-commit references swept before push.

A new auto-memory entry recording the rule (`Don't expose
user-personal examples in source code`) so this category of
mistake doesn't recur in future sessions.

## Implementation files

  - `skylakegrep/src/proactive.py` — new module (~430 lines).
  - `skylakegrep/src/cli.py` — search_cmd hooks
    `proactive.run_enhancers_parallel(...)` after main results
    render and before the low-confidence quality hint. Quality
    hint is suppressed when proactive surfaced anything (no
    point nagging when the user got more info).
  - `tests/test_proactive.py` — 23 new tests covering the four
    contract guarantees (should-fire gating, budget enforcement,
    failure isolation, kill-switches) plus filename_extend
    regression coverage.

Test suite: **188 / 188 passing** (16 subtests). Added 23 new
tests for the proactive framework.

## Latency contract

**Normal queries pay zero extra cost** — the should-fire gate
returns False unless the enhancer's preconditions hold. Eligible
enhancers (the user-reported cold-start case) get a parallel budget
of 500 ms total; whatever finishes within that wall clock gets
surfaced.

| Scenario | 0.2.6 | 0.2.7 |
| --- | :-: | :-: |
| Normal query (top hit confident) | 0.5 s | **0.5 s** (proactive doesn't fire) |
| Filename lookup, 0 in-project hits | 0.5 s + "no match" | **~0.7-0.9 s** + parallel `find` results |
| Worst case (all enhancers fire) | n/a | **≤ 1.0 s** (total budget cap) |

`SKYGREP_PROACTIVE_BUDGET_MS=0` disables the framework cleanly.

## Compatibility

  - Python ≥ 3.9 (unchanged)
  - Existing 0.2.0–0.2.6 indexes: no migration; proactive runs on
    top of cascade output without touching the index.
  - Bench numbers unchanged: 30 / 30 across Django + React +
    Tokio at ~14.6 s/q aggregate.

## Verifying the user-reported case

```bash
# from a directory where your target file isn't located:
$ skygrep "Where is my <filename>?"
... [main results — 0 hits]
[footer]

💡 Found N match(es) outside the current project root...
   📄 ~/Downloads/<filename>.pdf
   ...
```

The old behaviour ("No matches yet — semantic index is building")
no longer leaves the user wondering whether they're in the wrong
directory. The system already looked.

## Known follow-ups (not in 0.2.7)

  - **0.2.8** — additional proactive enhancers from the receipts
    table: `query_refinement`, `markdown_link_traverse`,
    `pdf_section_extract`, `git_history_related`. Each sized to
    its own individual_budget_ms.
  - **Phase C** — full intelligent-retrieval audit; tracked in
    [`docs/plans/2026-05-05-phase-c-audit.md`](plans/2026-05-05-phase-c-audit.md)
    + [`docs/plans/2026-05-05-phase-c-exploration.md`](plans/2026-05-05-phase-c-exploration.md).
  - Re-render `docs/assets/{benchmark,schema,hero-dark,og-image}.svg`
    to reflect bge-m3 defaults.
  - Re-run the self-test bench on bge-m3 and update
    `docs/token-benchmarking.md`.
  - Fix the GitHub Actions `PYPI_API_TOKEN` 403; manual `twine`
    flow continues to work.
