# skylakegrep 0.2.9 — release notes

`0.2.9` is a follow-up to `0.2.8` that handles the case the user
hit after the 0.2.8 ship: a Chinese mixed-language query like
`"我有没有跟\"<token>\"有关的文件？"` against a cold-start
directory still didn't trigger the proactive enhancer, because
the LLM router's rule-based fallback classified the query as
`intent=lexical` with `primary_token=""`. The 0.2.8 gate strictly
required `intent in (filename, mixed)`, so it didn't fire.

The fix adds two more eligibility cases to
`filename_extend_should_fire`, both Principle-1-compliant
(LLM-fed signals or content-shape morphology — never keyword
enumeration).

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact the maintainers.

## What changed

### `filename_extend_should_fire` now has three eligibility cases

```python
if decision is None:
    return False
intent = decision.intent
primary_token = decision.primary_token or ""

# Case 1 (existing): LLM classified intent as filename / mixed.
if intent in ("filename", "mixed"):
    eligible = True

# Case 2 (NEW in 0.2.9): LLM identified a primary_token even
# when intent is semantic / lexical. The user mentioned a
# specific identifier — fire so we can look for it as a filename.
elif primary_token and len(primary_token.strip()) >= 2:
    eligible = True

# Case 3 (NEW in 0.2.9): cascade returned 0 results AND the query
# contains a content-shape identifier (digits, dashes, CamelCase).
# Last-resort path for when the LLM is unreachable / produced
# low-confidence output but the query still has a clearly-shaped
# identifier we can filename-match against.
elif not results:
    candidate = _filename_token(query, decision)
    eligible = bool(candidate and _looks_like_identifier(candidate))
else:
    eligible = False
```

### `_looks_like_identifier(token)` helper

Token-shape check that asks: does this token plausibly name a
file? Three signals (any one suffices):

  - has digits (`<token>`, `task-001`, `v6.2`)
  - has internal punctuation (`foo.bar`, `my-file`, `snake_case`)
  - mixed case (`CamelCase`, `PascalCase`)

This is the same family of signals the LLM router prompt uses to
score candidate `primary_token` choices ("Prefer tokens with
digits or unusual capitalisation"). It is **token morphology**,
not keyword enumeration — consistent with Principle 1.

The distinction matters: a hand-curated list of trigger phrases
(`"where is" / "在哪" / "找一下" / "我的"`) was the 0.2.7 lapse.
A regex on the *shape* of the user's tokens is structural, not
keyword-based.

### Why this matters: the LLM-down fallback case

The user's screenshot showed `intent=lexical, primary_token=""` —
the rule-based classifier in `llm_router._rule_based_decision`.
This happens when:

  - Ollama isn't running
  - `qwen2.5:3b` isn't pulled
  - The LLM call timed out
  - The router cache had a stale entry from before 0.2.6

In those cases the rule-based classifier produces an `intent`
but explicitly leaves `primary_token=""` (it defers token
selection to `auto_index.filename_shortcut` which extracts its
own). Our 0.2.8 gate rejected this case because `intent` was
neither `filename` nor `mixed`, leaving the user with the bare
"No matches yet" notice.

0.2.9's case 3 catches this: when results are empty AND the
query has an identifier-shape token, fire anyway. Token-shape
detection is the same content-agnostic morphology check the
LLM router prompt uses internally, just lifted into our code.

## Implementation files

  - `skylakegrep/src/proactive.py` — new `_looks_like_identifier`
    helper and rewritten `filename_extend_should_fire` with
    three eligibility cases.
  - `tests/test_proactive.py` — 4 new tests:
    - `test_fires_when_llm_set_primary_token_even_on_semantic_intent`
    - `test_fires_on_zero_results_with_identifier_shape_token`
    - `test_does_not_fire_on_zero_results_pure_natural_language`
    - `test_does_not_fire_when_intent_is_lexical_and_no_identifier`
  - `pyproject.toml`, `README.md`, `docs/index.html` — version
    bump + What's-new entry.

## Compatibility

  - Python ≥ 3.9 (unchanged)
  - Existing 0.2.0–0.2.8 indexes: no migration.
  - Bench numbers unchanged: 30 / 30 across Django + React +
    Tokio at ~14.6 s/q aggregate.
  - Test suite: **193 / 193 passing** (16 subtests). Added 4 new
    tests for the expanded gate.

## Verifying the user's exact case

```bash
$ cd ~/Documents
$ skygrep 'do I have files related to <token>?'
[0 main results — cold-start, no semantic index]

💡 Found N match(es) outside the current project root...
   📄 ~/Downloads/<filename>.pdf  · 2300 KB
   ...

[0.7s · ripgrep cold-start + 1 proactive · index building in background]
```

Even when:

  - The semantic index isn't built (cold-start)
  - The LLM router is unreachable (rule-based fallback runs)
  - The classifier returned `intent=lexical, primary_token=""`

the gate's case 3 catches the identifier-shape token (`<token>` —
has digits) and fires `filename_extend` against `~/Downloads`,
`~/Desktop`, `~/Documents`.

## Known follow-ups (not in 0.2.9)

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
  - Improve `_rule_based_decision` to populate `primary_token`
    using the same token-shape scoring (would let case 1 fire
    via `intent=mixed` more often when the LLM is unreachable).
  - Fix the GitHub Actions `PYPI_API_TOKEN` 403; manual `twine`
    flow continues to work.
