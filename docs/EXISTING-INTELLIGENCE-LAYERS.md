# RED LINE — existing intelligence layers in skylakegrep

> **THIS DOCUMENT IS AUTHORITATIVE.** Before designing any change to
> retrieval, routing, ranking, indexing, or query handling — read this
> page first and explicitly state which existing layer(s) the change
> touches, augments, or replaces. The user has called out three times
> that I keep designing patches that ignore prior intelligence; that
> turns improvements into regressions. **NO RETRIEVAL CHANGE LANDS
> WITHOUT GOING THROUGH THE PRE-FLIGHT CHECKLIST AT THE BOTTOM.**

skylakegrep is not "cosine + maybe rerank." It is a **multi-channel
intelligence system** built up over 0.1.x – 0.5.x. Each layer was
introduced because a specific failure mode of an earlier-only design
was found and fixed. Removing or bypassing a layer without
acknowledging that failure mode means re-incurring it.

---

## The full layer catalog

### Tier 0 — query understanding (before any retrieval)

#### LLM router (`llm_router.py`)

- `route_query(query, conn, use_llm=True)` → `RouterDecision`.
- Uses `qwen2.5:3b` (configurable via `SKYGREP_LLM_ROUTER_MODEL`)
  to classify intent: `filename` / `lexical` / `semantic` / `mixed`.
- Returns `skip_cascade`, `skip_lexical`, `skip_filename`,
  `intent`, `primary_token`, `confidence`, `reason`, `source`.
- Three-layer fallback: SQLite per-session cache → LLM → rule-based
  `intent.classify_intent` from v0.14.0 → safe default
  `intent="mixed"` (every tier runs).
- 0.5.0+: ALSO exposes `infer_candidate_paths(query, tree_summary)`
  for lazy cold-start dir picking.
- 0.5.3 critical bug fix: `keep_alive` was being sent as
  string `"-1"` which Ollama parses as a duration and rejects with
  HTTP 400 — silently zero-ed every router call across 0.5.0–0.5.2.

#### Rule-based intent classifier (`intent.py`)

- `classify_intent(query)` — fallback when LLM unavailable.
- v0.14.0+ ALSO owns `merge_results` — hierarchical merge across
  filename / lexical / semantic tiers, ranked by detected intent.
- v0.14.0 changed routing from *mutually-exclusive tiers* (only one
  of filename / lexical / cascade runs) to *hierarchical merge*
  (all enabled tiers contribute). The user's "where is config
  file?" example surfaces BOTH the filename match (`config.py`)
  and the semantic chunk where config is loaded; v0.13.0 only
  returned the filename.

#### Out-of-scope detector + first-run nudge (`intelligent_cli.py`)

- `detect_out_of_scope(query)` — flags metadata queries
  ("我最近工作上的十个文件" / "list the largest 5 files") that want
  `git log` / `find -mtime` rather than content search. Prints a
  hint, still runs the search.
- `should_show_first_run_nudge` / `render_first_run_nudge` — once-
  per-project greeting explaining the rg-fallback + background
  index. Without this, first-time users think the tool is broken
  during the cold-start second.
- All disable-able via `SKYGREP_NO_HINTS=1` for CI / piped output.

---

### Tier 1 — fast structural / lexical channels (parallel, ~100 ms)

#### Filename shortcut (`auto_index.filename_shortcut`)

- When `route_query` flags the query as filename intent (or query
  has filename-shaped tokens like `package.json`, `Case42_*.pdf`),
  route to `find -iname '*token*'` and skip cosine entirely.
- Returns the `chunks`-table dict shape so it composes with merge.
- Conservative: requires identifier-shape token (digits / `._-` /
  mixed case). "where is auth?" doesn't trigger; "where is
  package.json?" does.

#### Lexical shortcut (`auto_index.lexical_shortcut`)

- Four-condition gate (all must hold):
  1. ≤ 3 non-stop-word query tokens.
  2. rg returns 1 – 30 candidate files.
  3. ≥ 1 path encodes ≥ 1 query token.
  4. Files cluster in ≤ 5 distinct parent dirs.
- If all hold → returns rg results directly, skip cascade. Else
  return None and let cascade run.
- Conservative on every dimension; semantic recall never sacrificed
  for routing speed.

#### Hybrid lexical prefilter (`hybrid.py`)

- `lexical_candidate_paths(query, root)` → small candidate file set
  via term extraction + ripgrep.
- Empirical receipt (Rust 16-task bench): rg in 0.4 s hits 16/16
  recall; cosine + cross-encoder alone in 50 s hits 14/16. The
  intent is "rg as high-recall first stage; cosine + small rerank
  as second stage on the rg-narrowed pool."
- Caller falls back to corpus-wide cosine when rg returns
  `< min_candidates` files (no usable surface-level overlap).

#### Tree-sitter symbol channel (`symbol_channel.py` + `storage.populate_symbols`)

- **INDEPENDENT retrieval channel — not a cosine booster, not a
  fallback.** Goes straight to a tree-sitter-built `symbols` SQLite
  table that maps `file → list[function/class/method names]`.
- `symbol_channel_search(conn, query_text, top_k)` — extracts
  identifier-shape tokens from query, looks up files DEFINING those
  symbols, returns top chunks from those files.
- `symbol_match_boost` — also exposed as a *cosine booster*: when
  cosine returns candidates, the booster adds points for files
  whose symbol table matches query terms.
- `_rrf_fuse([symbol_channel, cosine, …])` — reciprocal-rank-fusion
  of multiple ranked lists into one. THIS is the merge primitive
  for "use multiple channels equally".
- 0.4.x receipt: "0.4.1 reported 3/5 hit rate" was actually
  cascade-only with empty `symbols` table. Symbol channel silently
  no-ops when the table is empty — the published 30/30 OSS recall
  CRITICALLY DEPENDS on `populate_symbols` having run. **Don't
  bench cascade in isolation; the headline accuracy is the FULL
  pipeline.**

#### rg fallback (`auto_index.rg_fallback_results`)

- Plain ripgrep result wrapper for the cold-start path (no index
  built yet).
- 0.5.3: hard-coded `--sort path` for determinism — earlier
  multithreaded rg output order randomised the gate decisions.

---

### Tier 2 — σ-adaptive cascade (warm path, ~100 ms – 60 s)

#### `storage.cascade_search`

- Two-step:
  1. Cosine top-K cheap (~100 ms).
  2. If σ-gap (top1 – top2 file-mean cosine) > τ → exit cheap.
     If σ-gap < τ → escalate to cross-encoder rerank.
- `τ` is **adaptive**, not a fixed constant: derived from the
  noise floor of the score distribution. `CASCADE_TAU_FLOOR` and
  `CASCADE_K_SIGMA` are the only two constants here, and the lazy
  module also reuses them — **zero new hyperparameters in 0.5.x
  is contractual**.
- σ-gap is a Bayesian-evidence proxy for "are the top candidates
  well-separated?". High = cosine trusted. Low = either ambiguous
  or no signal.

#### Recovery hook (`storage.maybe_start_recovery`)

- Probes the embedder's current dim. If it differs from the index's
  stored fingerprint, spawns a background daemon that re-embeds
  stale chunks in mtime-DESC order.
- Search returns INSTANTLY with whatever has been recovered;
  `_filter_to_matching_dim` hides stale rows so the cascade never
  sees mixed dims. Recovery progresses across queries.

#### Symbol-aware boost inside cascade

- `cascade_search(use_symbol_boost=True)` (default on) calls
  `symbol_match_boost` AFTER the cosine round to lift scores for
  files whose symbol table matches query tokens. This is what makes
  "where does cascade gate logic live" find `storage.py` (defines
  `cascade_search`) before `cli.py` (just imports it).

#### HyDE escalation

- When cascade decides to escalate (σ-low), it can also invoke
  HyDE — ask the LLM to write a hypothetical answer matching the
  query, embed THAT, and use it as a second query vector. Helps
  vocabulary-mismatch cases where the user's phrasing doesn't
  match how code is named.

#### Cross-encoder rerank (`reranker.py`)

- The expensive escalation step inside cascade. Re-scores cosine
  candidates with a cross-encoder (BAAI/bge-reranker-v2-m3 by
  default). 10 – 60 s on cold load, ~1 s warm.
- 0.6 candidate (per the just-revised plan): when σ-gap is in the
  noise band (no cosine signal at all), DON'T escalate to rerank —
  rerank is also embedding-based and won't recover signal.
  Instead fall back to symbol_channel + filename_shortcut +
  lexical_shortcut RRF. Rerank is for σ-MEDIUM (genuinely ambiguous
  but with signal), not σ-zero (no signal).

---

### Tier 3 — proactive umbrella (parallel speculative, 0.5.6+)

This is the conceptual layer documented separately in
[`docs/proactive-umbrella-framework.md`](proactive-umbrella-framework.md).
**Always parallel with cascade, never sequential.**

#### `proactive.filename_extend`

- `find -iname '*token*'` over `SKYGREP_PROACTIVE_DIRS` (0.5.2+;
  default `~/Downloads:~/Desktop:~/Documents` for back-compat).
- Hits the answer in ~100 ms when the answer is a filename match
  in personal dirs (PDFs, docx, etc.) and not a code search.

#### `proactive.recovery_progress_hint`

- When the embedder-dim recovery worker is mid-pass, surfaces a
  hint that "the index is being re-embedded, results will improve
  in N queries / M minutes." Fires only when σ-gap is bad AND
  recovery is in progress. Without this, users see a degraded
  result and assume the tool is wrong.

#### `proactive.run_enhancers_parallel`

- Framework: each registered enhancer has a `should_fire`
  predicate, an `execute` function, and an `individual_budget_ms`.
- Runs eligible enhancers in a `ThreadPoolExecutor` with the
  per-enhancer budget; over-budget calls are cancelled.
- 0.5.6: launched BEFORE cascade in the warm path so its results
  stream first; `should_fire` is given empty results so it always
  triggers (let the merge step de-dupe).

#### `lazy_indexer.lazy_explore_cold_start`

- Cold-start lazy semantic: walk the cwd tree, deterministic
  dir-token picker + LLM-routed dir picker, expand picked dirs
  into seeds (top dir gets 8 files; tier-rank: `__init__.py` first,
  non-numeric next, numeric last), regex import diffusion adds
  ~10 neighbours, ThreadPool-parallel I/O, single Ollama batch
  embed, σ-validated cosine top-K with confidence label.
- 0.5.3 fixes summarised in module docstring; reuse them, don't
  re-invent.

#### `lazy_indexer.lazy_explore_cross_folder`

- Same machinery but over `SKYGREP_PROACTIVE_DIRS`. Per-root cap
  was 30 000 files in 0.5.3, lowered to 5 000 in 0.5.6 because
  macOS `~/Documents` + iCloud sync made the walk alone exceed
  60 s on the user's machine.

---

### Tier 4 — content / multi-rep / answer

#### Multi-resolution retrieval (`storage.search`)

- `rank_by="chunk"` returns top-K chunks with per-file diversity
  cap. `rank_by="file"` returns one best chunk per file. The
  CLI-default depends on intent (filename → file-rank; code
  semantic → chunk-rank).

#### doc2query enrichment (`enrich.py` — Layer L3)

- For each chunk: ask the LLM for a 1–2 sentence "what user-facing
  concept is this?" description, append to chunk text, re-embed.
- Resumable (commit-per-chunk, picks up `enriched_at IS NULL`).
- Bridges vocabulary mismatch by enriching code-shaped chunks
  with concept-shaped descriptions.

#### Cross-encoder rerank pool (`storage._cascade_rerank`)

- Pool size adaptive (default 30, configurable via `--rerank-pool`).
- Optional via `--rerank` flag.

#### Reference-graph PageRank tiebreak

- `reference_graph.populate_graph_table` builds the
  `file → file` import edge table.
- `cascade_search` uses the resulting PageRank score as a tiebreak
  for cosine-tied candidates (Δ ≤ 0.01). The footer's
  `tied (Δ=0.011)` line is this firing.
- 0.4.0/0.4.1 disaster: the graph was used as a *primary
  expansion channel* (`graph_expand`) which silently corrupted
  `merge_results` because expanded dicts lacked the `snippet` key.
  0.4.2 hot-fix demoted graph back to **tiebreak-only**. **Never
  re-promote the graph to primary without the receipt.**

#### Answer synthesis (`answerer.py`)

- `OllamaAnswerer.answer(query, results)` — when `--answer` is
  passed, synthesizes a 1-paragraph LLM answer from the top-K
  chunks plus a "Sources:" list.
- Used by `--answer` and by HyDE escalation inside cascade.

---

### Tier 5 — index management

#### `auto_index.first_time_index`

- Bootstrap walk + chunk + embed + symbol-extract + graph-build
  on first run.

#### `auto_index.spawn_background_index`

- Fork-detached index when cwd is unindexed and `--auto-index` is
  on (default). User's first query falls back to rg in < 1 s while
  the background index builds.

#### `auto_index.incremental_refresh`

- mtime-based refresh on every query. Re-embeds files whose mtime
  changed. Throttled by `_refresh_throttle_from_env()` so successive
  fast queries don't refresh redundantly.

#### `auto_index.is_index_ready` / `is_index_building`

- Routing primitives: ready → cascade. Building / absent → rg
  fallback. **`cli.py` keys off these signals; both must be
  preserved by any routing change.**

---

### Tier 6 — UX / observability

#### Telemetry footer (`cli.py`)

- The trailing `[Ts · path=… · σ-gap=… · pool=… · index=…]` line
  is the user-facing "what just happened" signal. Documented at
  `docs/cli.html#reading-telemetry`. **Any new tier MUST emit a
  field here** so the user can attribute the answer to a route.

#### Streaming preliminary blocks (0.5.5+)

- `▾ preliminary keyword + filename matches` — fn / rg hits
  before cascade.
- `▾ proactive umbrella · home-dir filename matches` — proactive
  enhancers before cascade (0.5.6).
- `▾ preliminary cascade matches` — cascade hits before cross-folder.
- `▾ refined matches from sibling-folder semantic search` — late.
- Each block has a route + quality label (`filename_extend, ~100 ms,
  pure filename glob, no semantic understanding`).

#### Hard timeouts

- cascade ≤ 30 s (0.5.6, worker thread + dedicated SQLite conn).
- cross-folder ≤ 8 s (0.5.6).
- proactive per-enhancer `individual_budget_ms` (default 1500).

#### Progressive stderr in lazy (0.5.3+)

- `🔍 / 🌊 / 💧 / ⚡` lines from `lazy_indexer._stderr_progress` so
  the user sees activity during the 5–30 s embed pass.

---

## Interaction matrix (who feeds whom)

```
Query
  ├─ LLM router ──→ RouterDecision (intent, primary_token, skip flags)
  │
  ├─ filename_shortcut (cwd, ~100ms) ───┐
  ├─ lexical_shortcut  (cwd, ~100ms) ───┤
  ├─ rg_fallback       (cold start) ────┤  parallel @ t=0
  ├─ proactive.filename_extend          │  (Tier 1 + Tier 3)
  │   (~/Downloads etc, ~100ms-1s) ─────┤
  ├─ symbol_channel_search              │
  │   (tree-sitter symbols, ~100ms) ────┤
  ├─ lazy_explore_cwd (cold ~5-30s) ────┤
  ├─ lazy_explore_cross_folder          │
  │   (sibling, ~5-8s) ─────────────────┤
  │                                     │
  └─ cascade_search ────────────────────┤
       (cwd indexed, ~100ms-30s,        │
        adaptive σ + rerank + symbol    │
        boost + graph tiebreak) ────────┘
                                        │
                                        ▼
                            intent.merge_results / RRF fuse
                                  (dedupe by path,
                                   intent-aware ranking)
                                        │
                                        ▼
                    Telemetry footer + render + answerer
```

---

## When σ-gap is in the noise band — the correct fallback chain

The user (2026-05-06) explicitly corrected an earlier mistaken
proposal. **Don't write "if σ-gap low, just return cosine top-K."**
The correct fallback when cascade has no cosine signal:

```
σ-gap > τ_high     → cosine-cheap top-K (~100ms) ✓
σ-gap > τ_low      → escalate to cross-encoder rerank (~10-30s) ✓
σ-gap < τ_low ≈ 0  → STRUCTURAL FALLBACK:
                       symbol_channel_search (tree-sitter) ┐
                       filename_shortcut                   │ RRF
                       lexical_shortcut                    │ fuse
                                                           ┘
                       → return RRF top-K (<500ms)
                       → ONLY IF all empty: "no signal" message
```

Rerank is for σ-MEDIUM (ambiguous candidates). It's not for σ-ZERO
(no signal at all) — both rerank and cosine are embedding-based, so
no-signal-cosine + no-signal-rerank wastes 30 s for the same nothing.

---

## Receipts — when each layer was wrong

These are the lessons. Cite them in any change that proposes to
remove a layer.

- **0.4.0/0.4.1: graph_expand promoted to primary channel** →
  `KeyError 'snippet'` in `cli.merge_results`, 0.4.2 hot-fix
  demoted graph back to tiebreak-only.
- **0.5.0–0.5.2: lazy treated as "cold-start fallback"** → user's
  warm query took 12:50; 0.5.6 fixed via parallel umbrella.
- **0.5.0–0.5.2: keep_alive=`"-1"` HTTP 400** → silent zero-ing of
  every LLM router call, 0.5.3 fixed.
- **0.4.x: bench used `cascade_search` direct API** → reported
  3/5 hit rate while real CLI gave 30/30. Always test through the
  ACTUAL user path (CLI), not internal APIs.
- **0.5.0 lazy bench**: claimed 4/10, was a python-API bench
  bypassing the merge. Real CLI gave 1/10. 0.5.3 fixed via
  deterministic dir picker + numeric-prefix penalty + LLM router
  unblock.
- **Pre-0.4.x graph hardcoded language extractors** → replaced by
  pluggable `reference_graph` registry; ALL retrieval changes
  should use the registry, not hard-code language.

---

## PRE-FLIGHT CHECKLIST — complete before any routing/retrieval change

Print these answers in the change's commit message or release notes.
Don't skip any. If a question doesn't apply, say "N/A — because Y".

1. **Which layer(s) does this change touch?** (Name from the
   catalog above. If "none of the above," stop and re-read the
   catalog.)
2. **Which OTHER layers does the change interact with at runtime?**
   (e.g. "modifies cascade σ-gap → also affects symbol_match_boost
   because the boost runs after cascade.")
3. **Does the change PRESERVE / AUGMENT / REPLACE the existing
   layer?**
   - PRESERVE: behaviour unchanged for all existing call sites.
   - AUGMENT: new code path active under specific conditions; old
     path still default.
   - REPLACE: existing path removed. **Receipt required**: when
     was the existing layer wrong? what test demonstrates it?
4. **Was this layer historically promoted / demoted / removed?**
   (Check the Receipts section.) If yes, does the proposed change
   re-introduce a known regression?
5. **What hyperparameters does the change introduce?** Default
   answer must be ZERO. Reuse `CASCADE_TAU_FLOOR`, `CASCADE_K_SIGMA`,
   `seed_budget`, etc. The "zero new constants" contract is a
   hard rule.
6. **Does the change keep the FULL pipeline together for benches?**
   Benchmarks must run via the real `skygrep search` CLI, not
   `cascade_search` direct calls. Internal-API benches lie.
7. **Does the change emit a telemetry-footer field** so the user
   can see which route produced the answer?
8. **Hard timeout and streaming preliminary block?** If the change
   adds a slow tier, it MUST have a hard timeout AND emit a
   preliminary block before blocking.
9. **Is there a personal-data risk in any test artifact?** Check
   `feedback_no_personal_examples_in_code.md` rules.
10. **Receipts**: if the bench numbers move, capture them in the
    release notes — old vs new, real CLI, fresh DB.

---

## Where this document lives, and how to keep it fresh

- **This file**: `docs/EXISTING-INTELLIGENCE-LAYERS.md` — included
  in the wheel `docs/` dir, on the GitHub Pages site, in the repo.
- **Memory pointer**:
  `feedback_skylakegrep_existing_intelligence_layers.md` (memory
  index entry as RED LINE 0).
- **Update rule**: every release that adds, removes, or
  significantly modifies a layer MUST update this page in the same
  commit. Tag the section with the version (e.g. "0.5.6+",
  "0.4.x deprecated").
- **Reading rule**: the assistant must read this BEFORE designing
  any retrieval / routing / ranking change. Reference the
  pre-flight checklist explicitly in the proposal.
