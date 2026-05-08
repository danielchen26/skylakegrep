# skylakegrep 0.2.5 — release notes

`0.2.5` is a focused bug-fix + UX release. Three things, all
user-reported. **Critical bug**: pre-0.2.2 indexes were silently
filtered down to nothing because the recovery worker wasn't
detecting them (PDFs / older chunks invisible to the bge-m3
cosine query). The remaining two are smaller — keyword
stop-gap for the `昨天` (yesterday) miss in out-of-scope detection
and a hierarchical telemetry footer that's actually scannable.

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact the maintainers.

## Critical bug fix — recovery worker now triggers on pre-0.2.2 indexes

In 0.2.2–0.2.4, `recovery.detect_mismatch` had this logic:

```python
if stored is None:
    # Fresh index, or pre-0.2.2 index with no fingerprint yet —
    # record the current one. Do not trigger recovery.
    set_meta(conn, "embedder_fingerprint", current)
    return None
```

This was wrong for **pre-0.2.2 indexes that already had stale-dim
chunks**. The metadata table existed (it was added in 0.2.2), but
the `embedder_fingerprint` key was unset. The "fresh index"
short-circuit treated this as "nothing to do" and silently stamped
the current fingerprint, leaving the user's existing 768-d /
old-mxbai chunks invisible to the bge-m3 cosine query. The
`_filter_to_matching_dim` helper from 0.2.1 dutifully filtered
them out on every search, but no recovery worker spawned to
re-embed them.

**The user-visible symptom:** search results that should include
PDFs / older files came back without them, even though they were
indexed. Telemetry showed `Skipped 5104 file embeddings with stale
embedding dim` — the 0.2.1 warning fired, but the 0.2.2 "⟳
Embedder upgraded — re-embedding N stale chunks" notice never did,
because no recovery had been started.

**The fix in 0.2.5:** `detect_mismatch` now distinguishes
truly-empty fresh indexes (no fingerprint, no chunks → stamp
fingerprint, no recovery) from pre-0.2.2 indexes (no fingerprint,
stale chunks > 0 → trigger recovery). The recovery worker then
re-embeds in mtime-DESC order as designed in 0.2.2. Four new
regression tests in `tests/test_recovery.py` lock in the four
cases of (fingerprint set / unset) × (chunks at current dim /
stale dim).

**To recover an existing pre-0.2.5 index after upgrading:** just
run any `skygrep "<query>"`. The first query auto-detects the
mismatch, prints the `⟳ Embedder upgraded` notice, spawns the
background worker, and falls back to rg for the current query.
Semantic queries return progressively as files re-embed; full
recovery typically completes in 10–30 min on a mid-sized repo.

## Out-of-scope detection — `昨天` / `今天` / `last week` etc. now caught

The 0.2.4 `_METADATA_TOKENS` keyword list missed day-relative
recency tokens. The user's `我昨天打开过的十个文件` query
slipped through and ran semantic search instead of getting the
`git log` suggestion.

`0.2.5` adds:

  - Chinese day-relative: `昨天`, `今天`, `前天`, `上周`, `本周`, `刚刚`
  - Chinese verbs that imply mtime: `打开过`, `改过`, `编辑过`, `修改过`
  - English day-relative: `yesterday`, `today`, `this morning`,
    `last week`, `this week`

**This is a stopgap.** The `_METADATA_TOKENS` enumeration is the
wrong shape — adding a token every time a user reports a new
miss is the same engineering pattern that motivated the 0.2.0
content-agnostic refactor of `code_graph.py` (and the still-open
Phase C work to generalise `symbol_channel.py`). The principled
fix lives in **0.2.6**: extend `llm_router.route_query()`'s LLM
prompt to classify `scope: content / recency / size / listing /
unknown` as part of the same call that's already running for
retrieval-intent classification — no extra latency, language- and
phrasing-agnostic understanding. The keyword list will be
demoted to **offline fallback** for when the LLM is unreachable.

This anti-pattern is now permanently recorded as Principle 1 in
[`docs/PRINCIPLES.md`](PRINCIPLES.md) along with the receipts
table of past lapses.

## Hierarchical telemetry footer

The 0.2.2–0.2.4 telemetry footer was one long `·`-separated line:

```
[0.722s · path=cosine-cheap · router=fallback-rules · intent=mixed (0.60) · 0 filename + 0 lexical · σ-gap=0.0582 ≥ τ=0.0358 (adaptive) → high-confidence early-exit · index 29s ago · 5107 files · L2 symbols on · graph prior on · quality=BEST]
```

User feedback was correct — this is a wall of text, hard to scan,
not hierarchical. `0.2.5` rewrites it as a multi-line block with
category labels:

```
✓ 0.722s · quality=BEST
   path     : cosine-cheap (high-confidence early-exit)
   router   : fallback-rules → intent=mixed (0.60)
   evidence : σ-gap=0.0582 ≥ τ=0.0358 (adaptive)
   pool     : 0 filename + 0 lexical · cascade
   index    : 29s ago · 5107 files · L2 symbols + graph prior
```

With recovery active:

```
⚠ 0.18s · quality=DEGRADED-recovery
   path     : cosine-cheap
   recovery : in-progress · chunks=1234/5107 · coverage=24% · ETA=14m
   index    : 29s ago · 5107 files
```

The `✓` / `⚠` glyph is the at-a-glance scannable signal —
`✓` means the result is full-quality semantic, `⚠` means
recovery is degrading some files. Set `SKYGREP_FOOTER_COMPACT=1`
to opt back into the old single-line format (for tooling that
parses the footer).

## Compatibility

  - Python ≥ 3.9 (unchanged)
  - Existing 0.2.0–0.2.4 indexes: no migration; recovery worker
    auto-triggers on first `0.2.5` search if stale-dim chunks
    exist.
  - Bench numbers unchanged from 0.2.4: 30 / 30 across Django +
    React + Tokio at ~14.6 s/q aggregate.
  - Test suite: 161 / 161 passing (16 subtests). Added 4 recovery
    regression tests + 6 day-relative recency tests.

## What's next — 0.2.6

The keyword stopgap in this release is explicitly a patch.
`0.2.6` does the principled refactor:

  - Extend `llm_router.route_query()`'s LLM prompt (qwen2.5:3b,
    already running on every query) to classify `scope: content /
    recency / size / listing / unknown` alongside the existing
    intent.
  - `RouterDecision` gains `out_of_scope_kind` and
    `out_of_scope_suggestion` fields.
  - `intelligent_cli.detect_out_of_scope` reads from
    `RouterDecision` first; falls back to `_METADATA_TOKENS` only
    when the LLM call is unavailable (offline / Ollama down /
    model not pulled).
  - The keyword list stops being the primary classifier and
    becomes the offline safety net.

This is the architecture the user repeatedly flagged as the right
shape — substrate before scaffolding, understanding over
enumeration — and is what `docs/PRINCIPLES.md` Principle 1 calls
for.

## Known follow-ups (not in 0.2.5)

  - **0.2.6** — LLM-based scope classification (above).
  - **`skygrep tour`** — interactive 5-step walkthrough for
    first-time users.
  - **Phase C** — full intelligent-retrieval audit; tracked in
    [`docs/plans/2026-05-05-phase-c-audit.md`](plans/2026-05-05-phase-c-audit.md)
    + [`docs/plans/2026-05-05-phase-c-exploration.md`](plans/2026-05-05-phase-c-exploration.md).
  - Re-render `docs/assets/{benchmark,schema,hero-dark,og-image}.svg`
    to reflect bge-m3 defaults.
  - Re-run the self-test bench on bge-m3 and update
    `docs/token-benchmarking.md`.
  - Fix the GitHub Actions `PYPI_API_TOKEN` 403.
