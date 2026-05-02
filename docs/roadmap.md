# local-mgrep accuracy roadmap

This document records the planned improvements to lift local-mgrep retrieval
quality so that recall on unseen large repositories approaches
`@mixedbread/mgrep` cloud parity, while keeping the token-reduction advantage
over ripgrep.

It is a strict execution plan. Items are ordered by ROI; each item names the
concrete code change, expected lift, and verification step.

## Status (warp 16-task benchmark)

| Phase | warp recall | Status |
| --- | :-: | --- |
| pre-P0 baseline | 8/16 | — |
| P0-A (cross-encoder rerank) + P0-B (nomic-embed-text + asymmetric prefixes) | 9/16 | **DONE** |
| P1-C (chunk path/symbol prefix) + P1-E (tree-sitter dedup + max_chars 2000) | 10/16 | **DONE** |
| P1-D (BM25 + path-segment-exact bonus) | 10/16 (no net lift; reverted) | **ABANDONED** — surface-token shape on warp doesn't reward this; LLM-driven HyDE is the better lever |
| P2-F (HyDE) | 11–13/16 (mean ~12/16) | **DONE** |
| P2-F+ (deterministic HyDE seed, ``mxbai-rerank-large-v2``, non-canonical path penalty) | **14/16** stable | **DONE** — 2 misses remain (websocket / billing) |
| P2-Latency (measure daily-driver tradeoff) | base rerank + no HyDE = **10/16 @ 12.7 s** ; raw = 8/16 @ 3.3 s | **DONE** — speed × recall curve in `docs/parity-benchmarks.md` |
| P2-G (asymmetric query/document prefixes) | folded into P0-B | **DONE** |
| P2-H (configurable max-per-file / rerank-pool) | exposed as CLI flags | **DONE** |
| P3-I (ColBERT late interaction) | not started | future |
| P3-J (LoRA fine-tune of reranker) | not started | future |

## Why we are not at parity today

Five structural shortfalls in the current pipeline, ranked by their
contribution to the warp recall miss:

1. **No cross-encoder rerank.** `storage.py:247` ranks final results by a
   linear blend `0.8 · cosine + 0.2 · lexical`. Mixedbread's flagship product
   line is the rerank stage (`mxbai-rerank-large-v2`), not the embedding;
   their cloud advantage is rerank compute, not embedding quality.
2. **General-English embedding on Rust code.** `config.py:5` defaults to
   `mxbai-embed-large`, which is not trained on code. Token "microphone" is
   far in vector space from `AudioInput::new()` chunks.
3. **Chunk text has no path / filename / symbol prefix.** `indexer.py:222-234`
   stores raw chunk text only; embedder cannot tell that a chunk lives in
   `crates/voice_input/lib.rs` versus `crates/billing/checkout.rs`.
4. **Lexical score is set-overlap, not BM25, no path boost.**
   `storage.py:140-153` weights every token equally; identifier tokens in
   filenames get the same weight as common English words in chunk bodies.
5. **Tree-sitter chunker emits redundant nested nodes.** `indexer.py:191-206`
   recurses without pruning, so a function chunk, its containing impl block,
   and the containing module all enter the index. The `MAX_RESULTS_PER_FILE`
   = 2 cap is a band-aid for this.

The headline trade-off in benchmark #3 of `parity-benchmarks.md`
(869× fewer tokens, 50% recall on warp) is the mathematical consequence of
all five.

## Execution plan

### P0 — immediate (target: warp recall 8/16 → 12-14/16)

**P0-A: cross-encoder reranker as second-stage scorer.**

- New module `local_mgrep/src/reranker.py` wrapping a `CrossEncoder` from
  `sentence-transformers`. Default model
  `mixedbread-ai/mxbai-rerank-base-v2` (≈150 M params, CPU-friendly).
- Lazy import — basic `mgrep` install does not pull torch.
- `storage.search()` retrieves a wider candidate pool (default 50), the
  reranker scores `(query, chunk)` pairs, top-k by rerank score is returned.
- CLI flag `--rerank / --no-rerank` (default on when extras installed,
  graceful fallback when not).
- New optional dep group: `pip install -e ".[rerank]"`.

**P0-B: switch default embedding to `nomic-embed-text`.**

- Already present in user's Ollama install.
- Supports asymmetric `search_query: ` / `search_document: ` prefixes,
  giving us P2-G "for free" once enabled.
- Add a runtime warning when the on-disk index vector dim does not match the
  current model's vector dim, with a clear `mgrep index --reset` instruction.

**Verification.** Existing 14 unit tests pass; `agent_context_benchmark.py`
on the local-mgrep self-test recall stays 30/30 with rerank enabled;
`parity_vs_ripgrep.py --tasks benchmarks/cross_repo/warp.json` on warp
re-indexed under nomic-embed-text + rerank lifts mgrep recall above 12/16.

### P1 — within one week (target: warp recall 14-16/16)

**P1-C: prepend `[file: …] [lang: …] [symbol: …]` header to chunk text
before embedding.**

- Modify `indexer.prepare_file_chunks` to wrap chunk body with a structured
  prefix.
- `extract_code_chunks` uses tree-sitter to recover the enclosing symbol
  name (function / impl / class / module) when available.
- The same prefix is also stored on the chunk row so the reranker sees it.
- Requires re-indexing to take effect (vector dim does not change).

**P1-D: replace token-set lexical with SQLite FTS5 + BM25, weighted columns.**

- `storage.init_db` adds an external-content FTS5 virtual table over the
  `chunks` table, with separate columns for `path`, `symbol`, and `chunk`.
- BM25 weighting boosts `path` and `symbol` columns ≥ 4× the body column,
  giving filename and identifier tokens IDF-correct weight.
- `lexical_score()` becomes a SQL FTS query; combined with cosine in the
  same hybrid blend.

**P1-E: fix tree-sitter chunker emit / dedup.**

- `walk()` only emits a node if no descendant qualifies, OR only emits leaves
  when configured. Prefer the most semantically meaningful enclosing
  function / class / impl block.
- Increase `max_chars` from 1000 → 2000 to actually use the embedding
  model's 512-token window.
- Remove the implicit `MAX_RESULTS_PER_FILE = 2` band-aid; lift the cap to a
  configurable default (5) once nested duplication is fixed.

### P2 — polish (target: hold ≥15/16)

**P2-F: HyDE for natural-language queries.**

- When the query is detected as natural language (no `::`, no `()`, no
  obvious identifier tokens, length > 8 words), call the local Ollama LLM
  to generate a short hypothetical code answer, then embed *that* instead
  of the raw query.
- Add `--hyde / --no-hyde` flag (auto-detected by default).
- New `answerer.OllamaAnswerer.hyde(query)` method.

**P2-G: asymmetric query / passage prefixes.**

- For `nomic-embed-text` and other models that document
  query / passage separation, prepend `search_query: ` to query inputs and
  `search_document: ` to document inputs.
- Lookup table in `config.py` keyed by model name.

**P2-H: expose `MAX_RESULTS_PER_FILE` and `RERANK_POOL` as CLI / env.**

- `--max-per-file` and `--rerank-pool` flags.
- Env vars `MGREP_MAX_PER_FILE`, `MGREP_RERANK_POOL`.

### P3 — long-term

**P3-I: ColBERT-style late interaction.** Multiple per-chunk vectors,
MaxSim against query token vectors. Index size ~10× larger; recall lifts on
hard semantic queries. Implementation requires a separate vector store
(e.g. `pylate`, `colbert-ai`) — not pursued until P0–P2 are exhausted.

**P3-J: task-fine-tuned reranker.** Use the 30-task self-test
+ synthetic question-passage pairs to LoRA-fine-tune
`mxbai-rerank-base-v2`. Self-host the fine-tuned head as a default override.

## Verification protocol per phase

After each P-level lands:

1. `pytest -q` — all existing unit tests pass.
2. `.venv/bin/python benchmarks/agent_context_benchmark.py --top-k 10
   --summary-only` — recall stays 30/30 on the local-mgrep self-test
   (regression guard).
3. `.venv/bin/python benchmarks/parity_vs_ripgrep.py --top-k 10
   --summary-only` — same self-test, with the real-rg comparison.
4. Re-index warp once per phase, then
   `.venv/bin/python benchmarks/parity_vs_ripgrep.py --root /path/to/warp
   --tasks benchmarks/cross_repo/warp.json --top-k 10 --summary-only`,
   and append the phase's mgrep recall and token-reduction numbers as a
   new row in `docs/parity-benchmarks.md`.
5. Commit, push, update `docs/parity-benchmarks.md` headline table.

The headline check after P0 (A + B) is whether warp mgrep recall reaches
≥ 12/16 with no more than a 4× rise in mgrep total tokens. If yes, proceed
to P1. If no, diagnose which queries still miss and revisit P0 selection
before adding P1 changes.
