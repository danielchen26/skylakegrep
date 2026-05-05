# skylakegrep 0.2.6 — release notes

`0.2.6` ships **the architectural fix** behind the `0.2.5` keyword
stopgap. The user repeatedly flagged the anti-pattern: enumerating
tokens (`昨天`, `今天`, `last week` …) is a patch you can never
finish. The principled answer is to use the **understanding** layer
that already runs on every query — the local LLM router — and
demote the keyword list to an offline safety net. This release
implements exactly that, and updates `docs/PRINCIPLES.md` Principle
1 ("Understanding > Enumeration") receipts table to mark
`_METADATA_TOKENS` as ✓ shipped.

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact <chentianchi@gmail.com>.

## What changed

### Out-of-scope detection — LLM understanding now primary, keyword list demoted to offline fallback

`RouterDecision` gained an `out_of_scope` field. The existing LLM
router prompt (which has been running on every query since 0.15.0
for retrieval-intent classification) now also classifies the query's
scope into one of:

  - `"none"` — semantic / lexical / filename content search
    (the common case)
  - `"recency"` — user wants files by modification time
    (`recent files`, `我昨天打开过的`, `last week's edits`, ...)
  - `"size"` — user wants files by size
    (`largest files`, `smallest config`, ...)
  - `"listing"` — user wants a flat listing or count
    (`list all py files`, `how many tests`, ...)

The router prompt was extended in place; the **same `qwen2.5:3b`
call** that classifies routing intent now also produces the
out-of-scope classification — **zero added latency**. The router
cache (`~/.skylakegrep/router_cache.db`) was extended to serialise
the new field; older cache entries are tolerated by filtering
unknown keys before reconstructing `RouterDecision`.

`intelligent_cli.detect_out_of_scope(query, decision=...)` is now a
two-layer detector:

  1. **Primary (0.2.6+):** if `decision.out_of_scope` is
     `"recency" / "size" / "listing"`, return the corresponding
     hint. If `decision.out_of_scope == "none"`, **explicitly
     return None even when the keyword list would have matched** —
     the LLM has full context and overrides the keyword heuristic.
  2. **Offline fallback:** when `decision is None` (caller is a
     non-CLI path) or `decision.out_of_scope is None` (LLM
     unreachable, rule-based fallback ran, or older cache entry
     without the field), the conservative keyword list still works
     so the hint isn't silently dropped.

### Why this is the principled fix

The user's `我昨天打开过的十个文件` query motivated this work.
The 0.2.5 stopgap added `昨天 / 今天 / 前天` to the keyword list,
which works for those exact words but fails on the next
unenumerated phrasing (`我前几天写的`, `the last sprint's edits`,
new languages, new vocab). The LLM-driven detector handles **all**
of those cases without us touching code, because qwen2.5:3b
*understands* the query. This is the same architectural shape as:

  - 0.2.0's `bge-m3` substrate (multilingual embedder replaces
    English-only `mxbai-embed-large`)
  - 0.2.0's `reference_graph.register_extractor()` (registry
    replaces hardcoded Rust/Python/JS/TS regex in `code_graph.py`)

`docs/PRINCIPLES.md` Principle 1 ("Understanding > Enumeration")
codifies this rule. The receipts table now lists `_METADATA_TOKENS`
as ✓ shipped in 0.2.6.

### What stays the same

  - The keyword list survives as the offline fallback. We did not
    delete it — when Ollama is down or `qwen2.5:3b` is missing, it
    still gives users the hint on `recent files` / `最近` / `largest`.
  - 0.2.5's recovery fix, hierarchical footer, day-relative tokens
    (`昨天`, `今天`, `前天`, ...) all stay in place. The day-relative
    tokens are now part of the offline fallback list.
  - 0.2.4's typo correction, low-confidence hint, first-run nudge
    behaviours unchanged.

## Implementation

  - `skylakegrep/src/llm_router.py`:
    - `RouterDecision.out_of_scope: str | None = None` (new field)
    - `_ROUTER_PROMPT` extended with the four-value enum + two
      example queries (`我昨天打开过的十个文件`, `list all the
      largest python files`)
    - `_llm_decision` parses & validates the new field; unknown /
      missing values become `None` so the caller falls back
    - `_cache_get` filters unknown keys from cached payloads
      (forward / backward compatibility for cache-format changes)
    - `_cache_set` serialises `out_of_scope` so subsequent calls hit
      the cache rather than re-running the LLM
  - `skylakegrep/src/intelligent_cli.py`:
    - New `_hint_for_kind(kind, *, reason)` helper — maps the
      LLM's classification kind to the concrete shell-command
      suggestion. Both LLM and keyword paths now share this
      mapping; the LLM only has to classify, not invent the
      command line.
    - `detect_out_of_scope(query, decision=None)` accepts the
      router decision; LLM-driven primary path + keyword fallback.
  - `skylakegrep/src/cli.py`:
    - The out-of-scope hint render moved from "before the LLM router
      runs" to "right after `decision` is computed" so we can pass
      `decision` through. Search still runs after the hint —
      non-blocking.
  - `tests/test_intelligent_cli.py`: 4 new tests covering
    LLM-classification scenarios (recency, size, none-overrides-
    keyword, none-falls-back-to-keyword-when-LLM-silent).

## Compatibility

  - Python ≥ 3.9 (unchanged)
  - **Cached router decisions from earlier versions** are
    auto-migrated: the cache reader now filters unknown keys, so
    older entries reconstruct as `RouterDecision(... out_of_scope=None)`
    and fall through to the keyword fallback. New queries write
    the new format.
  - Ollama / `qwen2.5:3b` requirement is unchanged — the LLM
    router has been mandatory since 0.15.0; 0.2.6 just gives it a
    new line item to classify.
  - Bench numbers unchanged: 30 / 30 across Django + React + Tokio
    at ~14.6 s/q aggregate.
  - Test suite: 165 / 165 passing (16 subtests). Added 4 new tests
    for the LLM-driven primary path.

## Verifying the fix on the user's exact case

```bash
$ skygrep "我前几天写的代码"
💡 Heads up: "我前几天写的代码" looks like a metadata query
   (LLM-router → out_of_scope=recency (user wants code from a
   few days ago — filesystem mtime query)). skygrep is a *content*
   search tool; the answer you probably want is:
       git log --name-only --pretty=format: HEAD~30..HEAD | sort -u | head -10
       or: find . -type f -mtime -7 -not -path '*/.*'
       or: git diff --name-only HEAD~10..HEAD
   Running semantic search anyway — set SKYGREP_NO_HINTS=1 to suppress.
```

Note that `前几天` is **not** in the keyword fallback list; the LLM
router catches it because it understands the query, not because we
enumerated the token.

## Known follow-ups (not in 0.2.6)

  - **Phase C** — full intelligent-retrieval audit; tracked in
    [`docs/plans/2026-05-05-phase-c-audit.md`](plans/2026-05-05-phase-c-audit.md)
    + [`docs/plans/2026-05-05-phase-c-exploration.md`](plans/2026-05-05-phase-c-exploration.md).
    The subagent-recommended next experiment (latency-variance
    ablation across `{baseline, no-rerank, no-L2-on-cheap,
    no-HyDE-on-symbol-q}`) is unblocked by 0.2.6 (cleaner router
    surface for the C5 "skip L2/L4 on cheap path" decision).
  - **`skygrep tour`** — interactive 5-step walkthrough; deferred
    from 0.2.4 / 0.2.5.
  - Re-render `docs/assets/{benchmark,schema,hero-dark,og-image}.svg`
    to reflect bge-m3 defaults.
  - Re-run the self-test bench on bge-m3 and update
    `docs/token-benchmarking.md` top-k 5 row.
  - Fix the GitHub Actions `PYPI_API_TOKEN` 403; manual `twine`
    flow continues to work.
