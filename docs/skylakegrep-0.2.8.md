# skylakegrep 0.2.8 — release notes

`0.2.8` is a focused bug-fix release for two real regressions
the user found in `0.2.7` within hours of shipping:

1. **The proactive framework never fired on cold-start queries.**
   The cli's index-not-ready branch returned early before the
   proactive enhancer hook, so users with a fresh / un-indexed
   directory got "No matches yet" instead of the parallel
   ``find`` over `~/Downloads` / `~/Desktop` / `~/Documents` that
   `0.2.7` was supposed to provide. This is the user-reported case
   exactly — and it didn't work.

2. **`filename_extend_should_fire` enumerated keyword phrases**
   as a fallback when `decision.intent` wasn't classified as
   filename. This is the **third** Principle-1 lapse in this
   project (after `code_graph.py` regex, `mxbai-embed-large`,
   and `_METADATA_TOKENS` keyword list). The user caught it
   immediately: "I see you're still using a lot of these keyword
   phrases. We shouldn't use keywords."

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact the maintainers.

## What changed

### 1. Proactive enhancer hook added to the cold-start path

The cold-start branch in `cli.search_cmd` (when the index isn't
ready and we fall back to `rg` + filename-shortcut) now calls
`proactive.run_enhancers_parallel(...)` before deciding what to
print. Two outcomes:

  - **Cascade returned 0 hits AND proactive returned 0 hits**
    → print the existing "No matches yet — index is building"
    notice. Same UX as before.
  - **Cascade returned 0 hits but proactive found something
    in `~/Downloads` / `~/Desktop` / `~/Documents`** → render
    the proactive output, append a `+ N proactive` tag to the
    cold-start footer, return.
  - **Cascade returned hits AND proactive added more** →
    render both blocks; the user sees the in-project hits at
    top and the cross-directory hits as a footnote.

Previously the cold-start branch had a hard `return` between the
"no matches yet" print and the proactive hook in the main
cascade path, so proactive never ran on the user-reported case the
framework was specifically built for.

### 2. `filename_extend_should_fire` is Principle-1-compliant

**Before (0.2.7):**

```python
def filename_extend_should_fire(query, decision, results):
    if results:
        return False
    intent = getattr(decision, "intent", "") if decision else ""
    if intent in ("filename", "mixed"):
        return True
    # Keyword fallback ⇐ THE ANTI-PATTERN
    q = query.lower()
    en_phrases = ("where is", "find me", "find my", "locate",
                  "show me my", "open my")
    zh_phrases = ("在哪", "在哪里", "找一下", "找到", "我的")
    return (any(p in q for p in en_phrases)
            or any(p in query for p in zh_phrases))
```

**After (0.2.8):**

```python
def filename_extend_should_fire(query, decision, results):
    if decision is None:
        return False  # No understanding available, refuse rather than enumerate.
    intent = getattr(decision, "intent", "")
    if intent not in ("filename", "mixed"):
        return False
    if not results:
        return True
    primary_token = _filename_token(query, decision)
    if not primary_token:
        return False
    token_lower = primary_token.lower()
    return not any(
        token_lower in Path(r.get("path", "")).name.lower()
        for r in results
        if r.get("path")
    )
```

**Why this is the correct fix:**

  - The LLM router (with rule-based fallback) ALREADY classifies
    intent on every query. Its output is the source of truth.
  - When the LLM is unreachable, the rule-based classifier still
    produces an `intent` — that's the offline path, NOT this
    gate's responsibility.
  - The keyword list was a third-time-shame Principle 1 lapse.
    `docs/PRINCIPLES.md` receipts table has been updated to mark
    `proactive.filename_extend_should_fire` as ✓ shipped in 0.2.8
    alongside the prior three lapses.

### 3. Added a results-without-token-match firing condition

A new behaviour shipped in 0.2.8 (was missing in 0.2.7): even
when the cascade returned non-empty results, if NONE of them
have the user's lookup token in their basename, fire proactive
anyway. This handles the case where the cascade returned
semantically-related noise (or `rg` matched random text files)
but didn't surface the actual file the user asked for.

Test in `test_fires_when_results_lack_token_match` locks the
behaviour in.

## Implementation files

  - `skylakegrep/src/proactive.py` — `filename_extend_should_fire`
    rewritten to trust `decision.intent` exclusively.
  - `skylakegrep/src/cli.py` — cold-start branch now calls the
    proactive enhancer hook before the early `return`.
  - `tests/test_proactive.py` — keyword-based tests removed
    (`test_fires_on_natural_language_lookup_phrase`,
    `test_none_decision_falls_back_to_phrase_check`); replaced
    with Principle-1 contract tests
    (`test_does_not_fire_when_intent_is_semantic`,
    `test_does_not_fire_when_intent_is_lexical`,
    `test_does_not_fire_with_none_decision`,
    `test_fires_when_results_lack_token_match`,
    `test_does_not_fire_when_results_have_token_match`).
  - `docs/PRINCIPLES.md` — receipts table updated.

## Compatibility

  - Python ≥ 3.9 (unchanged)
  - Existing 0.2.0–0.2.7 indexes: no migration.
  - Bench numbers unchanged: 30 / 30 across Django + React +
    Tokio at ~14.6 s/q aggregate.
  - Test suite: 190 / 190 passing (was 188 in 0.2.7; net +2 for
    the new firing-condition tests).

## Verification (the user-reported case in cold-start)

```bash
# In a directory whose index hasn't been built yet:
$ skygrep "Where is my <file>?"
[main results render — 0 in this case]

💡 Found N match(es) outside the current project root...
   📄 ~/Downloads/<file>.pdf  · 2300 KB
   ...

[0.7s · ripgrep cold-start + 1 proactive · intent=filename · 0 filename + 0 content · index building in background]
```

The "No matches yet — semantic index is building" message no
longer fires when proactive successfully extended the search.

## Known follow-ups (not in 0.2.8)

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
