# skylakegrep 0.2.12 — release notes

`0.2.12` is a focused fix for a `filename_extend` gate gap the
user found on Desktop. The 0.2.10–0.2.11 gate gave up when
`decision.primary_token` was empty (the rule-based router's
default when Ollama is unreachable) — even when the cascade
returned obvious noise (`Project.toml` / `Manifest.toml` files
matching `<token>` as a substring inside Julia package UUIDs but
having nothing to do with the user's actual <token> query).

Plus a new architecture plan filed under `docs/plans/` for the
**conversational session state** work the user asked about during
review.

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact the maintainers.

## What changed

### `filename_extend_should_fire` morphology fallback when LLM unavailable

```python
primary_token = (decision.primary_token or "").strip()
if not primary_token:
    candidate = (_filename_token(query, decision) or "").strip()
    if candidate and _looks_like_identifier(candidate):
        primary_token = candidate
if not primary_token:
    return False
# Now validate against results' basenames as before
```

Two behaviours unify:

  1. **LLM available** → trust `decision.primary_token` (Principle 1).
  2. **LLM unavailable** (rule-based fallback returns
     `primary_token=''`) → extract a candidate via
     `_filename_token`, accept only if `_looks_like_identifier`
     classifies it as identifier-shaped (digits / dashes / mixed
     case). This is content-shape morphology — same family of
     signals the LLM router prompt uses internally to score
     `primary_token` candidates — not keyword enumeration.

The 0.2.10–0.2.11 gate skipped this fallback, leaving the user's
Desktop scenario silently broken: cascade returned 5 lexically-
matched but semantically-wrong `.toml` files, gate's
`primary_token=''` check returned False without trying any
extension, user got a junk top-K with nothing pointing them at
the actual files in `~/Downloads`.

### `_looks_like_identifier` helper restored

The 0.2.9 → 0.2.10 simplification deleted this helper as part of
gate cleanup. Restoring it as a **classifier** for the
morphology-fallback path (not a gate): it decides whether a
candidate token is identifier-quality, and is only consulted
when LLM didn't supply `primary_token`.

Three signals (any one suffices):
  - has digits (`<token>`, `task-001`, `v6.2`)
  - has internal punctuation (`foo.bar`, `my-file`)
  - mixed case (`CamelCase`, `PascalCase`)

Pure English / Chinese natural-language tokens (`cascade`,
`work`, `find`, `auth`) all return False.

### End-to-end verification (logged before tagging)

Reproduced the user's exact scenario:

```
decision: intent='lexical' primary_token=''       (rule-based fallback)

# Synthetic results matching the screenshot — 5 toml files where
# "<token>" appeared inside a Julia package UUID, not the basename:
fake_results = [
    {'path': '/.../LotkaVolterra/Project.toml',  'score': 1.0},
    {'path': '/.../LotkaVolterra/Manifest.toml', 'score': 1.0},
    ...
]

should_fire(toml results) = True       ← was False in 0.2.10–0.2.11
should_fire(empty results) = True

run_enhancers_parallel:
  telemetry: {'fired': ['filename_extend'],
              'completed': ['filename_extend'],
              'timed_out': [], 'budget_ms': 2000, 'elapsed_ms': 1117}

  💡 Found 4 match(es) outside the current project root...
    /Users/example/Downloads/<filename-A>.pdf
    /Users/example/Downloads/<filename-A>.docx
    /Users/example/Downloads/<filename-B>.pdf
    /Users/example/Downloads/<filename-C>.pdf
```

This time the production code path actually surfaces the user's
files, even when the LLM router is unreachable. End-to-end
verified in development before commit, per the
0.2.7 → 0.2.10 lesson recorded in `docs/PRINCIPLES.md`.

### `docs/plans/2026-05-05-conversational-session-state.md`

The user also asked during 0.2.12 review:

> 他并没有基于上面给我我认为不对的答案继续给我答案这个问题
> 你怎么解决现在我们并没有让当前的状态 condition on 我们过去
> 问的问题对吧

Skygrep is currently stateless — every invocation re-routes,
re-retrieves, re-ranks from scratch. The user wants the next
query to be interpretable in the context of the previous one
(follow-up refinement, negative feedback, etc.).

This is a real architectural gap. The plan document captures:

  - Two conversational patterns (follow-up refinement vs.
    negative feedback)
  - Three implementation phases (S-1 query_history table; S-2
    LLM follow-up detection; S-3 actual conditioning logic)
  - Five open questions (session scope, privacy, decay, LLM
    cost, opt-in vs auto)
  - Cross-references to the existing Phase C and graph-prior
    plans

Not on a release schedule yet — depends on a measurement run +
explicit user mandate. Filed for the next architectural revisit.

## Test coverage

7 new + modified tests in `tests/test_proactive.py`, with one
specifically guarding the user's exact scenario:
`test_fires_on_lexical_noise_when_llm_unreachable`. The test
constructs a rule-based decision with empty `primary_token`,
feeds in synthetic toml-style noise results, and asserts the
gate fires.

Suite total: **201 / 201 passing** (20 subtests). Up from 200 in
0.2.11.

## Implementation files

  - `skylakegrep/src/proactive.py`:
    - `_looks_like_identifier` helper restored
    - `filename_extend_should_fire` morphology-fallback path
      added (LLM-available path unchanged)
  - `tests/test_proactive.py`:
    - `test_fires_on_lexical_noise_when_llm_unreachable` — guards
      the user's Desktop scenario
    - `test_does_not_fire_when_results_present_and_no_primary_token`
      — kept as the negative case (pure NL with no identifier
      shape doesn't fire, even with results present)
  - `docs/plans/2026-05-05-conversational-session-state.md` —
    new architecture document

## Compatibility

  - Python ≥ 3.9 (unchanged)
  - Existing 0.2.0–0.2.11 indexes: no migration.
  - Bench numbers unchanged.

## Known follow-ups (not in 0.2.12)

  - **Conversational session state** —
    [`docs/plans/2026-05-05-conversational-session-state.md`](plans/2026-05-05-conversational-session-state.md).
  - **Graph-prior folder inference** —
    [`docs/plans/2026-05-05-graph-prior-folder-inference.md`](plans/2026-05-05-graph-prior-folder-inference.md).
  - **Phase C** — full intelligent-retrieval audit; tracked in
    [`docs/plans/2026-05-05-phase-c-audit.md`](plans/2026-05-05-phase-c-audit.md).
  - More proactive enhancers (`query_refinement`,
    `markdown_link_traverse`, `pdf_section_extract`,
    `git_history_related`).
  - Re-render `docs/assets/{benchmark,schema,hero-dark,og-image}.svg`
    to reflect bge-m3 defaults.
  - Re-run the self-test bench on bge-m3 and update
    `docs/token-benchmarking.md`.
  - Fix the GitHub Actions `PYPI_API_TOKEN` 403; manual `twine`
    flow continues to work.
