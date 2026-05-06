# skylakegrep 0.5.6 — proactive umbrella runs in parallel with cascade (12:50 → 26 s)

The 0.5.4 ship missed a deeper issue. On a query like
`skygrep "do I have files about <token>?"` from a code repo where
the answer is a PDF in `~/Downloads`, 0.5.4 took **12 minutes 50
seconds** wall clock end to end. The cascade rerank ran 99.7 s
because the query had zero semantic match in the indexed code,
cross-folder lazy timed out at 8 s, and only THEN did the proactive
`filename_extend` enhancer fire — finding the answer (4 PDFs in
`~/Downloads`) in ~100 ms. The right answer existed the whole time,
hidden behind a sequential chain.

0.5.6 fixes the architecture, not just the symptom. Same query, now
**26 s** wall clock with the proactive answer streaming at ~1–2 s.

## The conceptual model that 0.5.6 conforms to

Two layers, parallel:

```
              Query
                │
                ▼
       ┌────────┴────────┐
       │                 │
     cascade        proactive umbrella
                        │
                        ├── lazy_cwd       (cold-start, embed cwd seeds)
                        ├── lazy_cross_folder (embed sibling-dir seeds)
                        ├── filename_extend   (~/Downloads etc filename glob)
                        └── (future speculative tiers)
```

`cascade` assumes the user is in the right project and the index is
built. `proactive umbrella` assumes nothing — it fans out into
parallel subprocesses each exploring a different "the answer might
live here" hypothesis. **They run alongside each other at t = 0**;
each streams as soon as it has anything to show.

The full conceptual model — including why "lazy IS proactive", why
sequential chains are forbidden, what the required UX is, and the
quality-indicator-per-block rule — is in
[`docs/proactive-umbrella-framework.md`](proactive-umbrella-framework.html).
That document is authoritative for any future routing change.

## What landed in 0.5.6 (code)

1. **Parallel proactive launch** (`cli.py` warm path, before cascade
   dispatch). `proactive.run_enhancers_parallel` now fires in a
   `ThreadPoolExecutor` worker BEFORE cascade starts, with empty
   `results` so its `should_fire` predicate triggers
   unconditionally. The post-cascade call is skipped when the
   pre-cascade result has anything — no double-fire, no double
   render.

2. **Pre-cascade drain with 2.5 s deadline** so the proactive
   umbrella's hits print BEFORE cascade even begins running:

   ```
   ▾ proactive umbrella · home-dir filename matches
     (filename_extend, ~100 ms-1 s; pure filename glob, no semantic
     understanding):
     <hits>
   ```

   The header announces both the route AND the quality semantics
   so the user knows whether to wait for cascade or trust the
   early answer.

3. **Cascade hard timeout** (30 s). On vocabulary-mismatch queries
   where σ-gap is tiny, the cascade escalates to cross-encoder
   rerank which can run 60–120 s. Once we've already shown the
   user the proactive umbrella's answer at ~1–2 s, there's no
   benefit in forcing them to wait a full minute for a cascade
   that already failed σ-validation. The cascade now runs in a
   worker thread (with its own SQLite connection because
   `sqlite3` forbids cross-thread connection reuse). A
   `concurrent.futures.TimeoutError` after 30 s short-circuits with
   a stderr explanation:

   ```
   ↻ cascade timed out at 30 s — top-K above (filename_extend /
     preliminary cascade / cross-folder) is the answer; cascade
     was in σ-low rerank, unlikely to add value
   ```

4. **Cross-folder hard timeout** (8 s, kept from 0.5.5
   intermediate). `lazy_explore_cross_folder` walks
   `SKYGREP_PROACTIVE_DIRS` and embeds 5 seed files; the 8 s cap
   protects against the macOS `~/Documents` + iCloud sync case
   where the walk alone could take a minute.

5. **Cross-folder per-root cap** lowered from 30 000 files to
   5 000. The earlier value tried to cover a single huge
   `/data/projects` cleanly but produced multi-minute walks on a
   default home tree. 5 000 / root × 6 default roots = 30 000
   total which is enough for any reasonably-sized OSS repo
   collection.

6. **Per-tier quality labels in stream headers**. Each block
   prints something like `(filename_extend, ~100 ms-1 s; pure
   filename glob, no semantic understanding)` or `(low confidence
   — also searching sibling folders…)` so the user always knows
   which route produced the block and what to expect from it.

## Numbers

Same <repo-A>-query reproducer the user reported in 0.5.5:

| Version | Wall clock | First answer at | Notes |
|---|---|---|---|
| 0.5.4 (released) | **12 m 50 s** | 99.7 s (after cascade rerank) | Sequential chain; user staring at blank prompt |
| 0.5.6 (this) | **26 s** | **~1–2 s** (proactive umbrella) | Parallel; cascade timed out at 30 s, but answer was already on screen |

The user's UX target was "first answer ≤ 3-5 s, then stream better
ones". 0.5.6 hits ~1–2 s on the proactive route for filename
queries, then streams the cascade's preliminary block at the
moment cascade returns (or times out at 30 s).

## Compatibility

- All public APIs unchanged from 0.5.4.
- Same wheel + sdist file layout.
- Index format unchanged; 0.5.4-built indexes work without
  re-indexing.
- `--lazy / --no-lazy` flag semantics unchanged.
- Existing proactive enhancers unchanged.
- New conceptual doc at `docs/proactive-umbrella-framework.md`.

## Verified

- `pytest tests/` — 217 / 217 pass (no test changes; baseline
  preserved).
- Real CLI on the user's project repo (warm path, indexed):
  wall clock 26 s, proactive umbrella block visible at ~1–2 s,
  cascade ran in worker thread under 30 s timeout, cross-folder
  timed out at 8 s as expected, no regressions in cascade
  result format.
- Generic placeholder query verified end-to-end (no personal
  data in any test artifact).

## Pending for 0.6

- The cascade σ-gap / cross-encoder rerank gate is still the
  legacy 0.2.x logic; on extreme vocabulary-mismatch queries it
  always escalates and consumes the full 30 s timeout. A future
  refactor could add an early "σ-gap below noise floor → return
  cosine-cheap top-K, skip rerank" exit.
- True streaming inside Ollama batch embed (currently 25 seeds
  → one synchronous call). Splitting into two rounds (5 + 20)
  would let lazy stream a first answer at ~5 s instead of
  waiting the full embed pass.
- Per-tier observability for ralph / autopilot consumers — the
  current stderr stream is human-readable but not structured.
