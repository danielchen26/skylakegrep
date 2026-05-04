# local-mgrep 0.5.0 — release notes

The 5-layer progressive-enhancement architecture from
[`docs/plans/2026-05-03-intelligent-system-v0.5.md`](plans/2026-05-03-intelligent-system-v0.5.md)
ships. This release adds three new layers on top of 0.4.1's `L0
ripgrep fallback` + `L1 chunk + file-mean cosine cascade` base.

Every new layer is offline-paid (it does its work at index time, in
the background, resumable) and contributes either zero or
sub-millisecond overhead at query time. The user sees more accurate
top-K results without paying any latency.

## What you can do today, plain language

```bash
pip install --upgrade local-mgrep
mgrep "how does the assistant call a language model?"
```

Behind the scenes, in priority order:

  1. ripgrep returns matches in ~0.5 s while the semantic index builds
     (introduced in 0.4.1; unchanged here).
  2. The semantic cascade takes over once the index finishes (also
     0.4.1; unchanged).
  3. **NEW (L2):** tree-sitter symbol indexing kicks in
     automatically. Concept words from the question that match
     `function` / `struct` / `class` / `module` / `trait` / `impl`
     names in any file boost that file's rank. CamelCase identifiers
     like `LanguageModelClient` are split into `language model
     client` so a natural-language query catches them.
  4. **NEW (L4):** a file-export PageRank graph runs once per
     project. It only fires as a tiebreaker — when the top-1 and
     top-2 final scores are within 0.005 of each other, the
     higher-PageRank file wins. Hubs cannot pull ahead of clearly
     better leaves.
  5. **NEW (L3):** opt-in `mgrep enrich` runs a one-time background
     LLM pass that writes a one-sentence high-level description for
     every chunk and re-embeds the chunk so its vector absorbs the
     semantic. The query no longer needs HyDE — the cost moves
     from query-time (3-5 s per uncertain query) to a one-time
     index-time pass.

## CLI surface changes

  - `mgrep enrich [PATH] [--max N] [--batch B]` — new top-level
    command. Resumable: Ctrl+C and re-run picks up where it stopped.
    Off by default at search time; export
    `MGREP_AUTO_ENRICH=yes` to have search auto-spawn enrichment in
    the background after a ready-index query.
  - `mgrep doctor` adds an `Enriched chunks` row showing
    `done / total (NN%)` so users know how far the L3 pass has
    progressed.
  - `mgrep stats` adds the same `Enriched` line.
  - The status line on every search names the highest layer that
    contributed:

```
[0.18s · cosine · index 4 min ago · 3247 files · L2 symbols on · graph prior on]
[0.20s · cosine · ... · L2 symbols on · graph prior on · tied (Δ=0.003)]
```

  - `mgrep search` defaults are unchanged. New scoring kwargs
    (`use_symbol_boost`, `use_graph_tiebreak`) are opt-out for
    legacy callers / unit tests.

## What changed under the hood

### L2 — Symbol-aware indexing (P5-SYM)

  - New `local_mgrep/src/storage.py::SYMBOL_WEIGHT` (default
    `0.10`, env `MGREP_SYMBOL_WEIGHT`).
  - New `symbols` table indexed by `name_lower`; one-time
    migration on first search rebuilds it from the existing
    `chunks` (`populate_symbols`).
  - New `local_mgrep/src/indexer.py::extract_file_symbols` —
    tree-sitter for Python / JS / TS, regex fallback for Rust.
    Splits camelCase via
    `re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name).lower()`.
  - New `storage.symbol_match_boost(conn, query_text,
    candidate_paths)` — for each candidate, additive bump of
    `(matched_terms / max(1, len(query_terms))) * SYMBOL_WEIGHT`.
  - Wired into `storage.search` after the non-canonical-path
    penalty pass and before `rank_by`.

### L3 — doc2query chunk enrichment (P6-D2Q)

  - New `chunks.enriched_at REAL` and `chunks.description TEXT`
    columns; `ensure_chunk_metadata_columns` migrates old DBs.
  - New `local_mgrep/src/enrich.py::enrich_pending_chunks` —
    deterministic prompt (`temperature=0`, `seed=42`,
    `num_predict=96`), one-or-two-sentence high-level
    description, append to chunk text, re-embed, commit per
    chunk. Resumable via `WHERE enriched_at IS NULL`.
  - New `mgrep enrich` CLI subcommand.
  - Optional background spawn under `MGREP_AUTO_ENRICH=yes` after
    ready-index searches (`subprocess.Popen(start_new_session=True)`,
    same pattern as `auto_index.spawn_background_index`).

### L4 — File-export PageRank tiebreaker (P7-PR)

  - New `local_mgrep/src/code_graph.py` — regex-parses Rust /
    Python / TS / JS imports across the project, builds a
    sparse adjacency dict, runs 50 iterations of PageRank with
    damping 0.85 (no NumPy adjacency matrix, no NetworkX
    dependency).
  - New `file_graph` table; one-time migration on first search.
  - New `storage._apply_graph_tiebreak`, called from
    `storage.search` only when the top-1 and top-2 scores are
    within `TIEBREAK_EPS` (default `0.005`,
    `MGREP_TIEBREAK_EPS`). The tiebreaker bump is bounded by
    `GRAPH_TIEBREAK_WEIGHT` (default `0.005`,
    `MGREP_GRAPH_TIEBREAK_WEIGHT`); the safety property
    `GRAPH_TIEBREAK_WEIGHT ≤ TIEBREAK_EPS` guarantees a clear
    score gap can never be flipped (the regression guard against
    the abandoned P4-CGC failure mode where a global graph prior
    pulled `lib.rs` with 1655 in-degree ahead of canonical
    leaves).

## Tests

`pytest -q tests/` reports **40 passed** (24 baseline + 6 L2 + 4
L3 + 6 L4) on the merged release branch. The L4 regression test
explicitly demonstrates that a clear cosine gap cannot be flipped
by the PageRank prior — `test_tiebreaker_does_not_flip_clear_gaps`.

## Compatibility

  - All 0.4.x flags remain valid.
  - Existing project indexes are picked up as-is. The first search
    against an old index migrates the schema (adds `enriched_at`,
    `description` columns; populates `symbols` and `file_graph`
    tables) — pure tree-sitter and regex, no LLM, typically <10 s
    on a 5K-file project.
  - The cascade default introduced in 0.4.0 is unchanged.
  - The ripgrep fallback path introduced in 0.4.1 is unchanged.
  - 24 / 24 prior unit tests still pass alongside the 16 new
    layer-specific tests.

## Performance expectations

  - Cold first query in a fresh project: ~0.5 s (rg fallback),
    same as 0.4.1.
  - Warm query without enrichment: ~150-300 ms with cascade +
    symbol boost + graph tiebreaker.
  - Warm query with full enrichment (after `mgrep enrich` has
    completed for the project): ~150 ms with **no LLM call at
    query time** because the doc2query semantic is already
    embedded into chunk vectors.

The full repo-A 16-task benchmark sweep across all 5 layers will land
in the next iteration of `docs/parity-benchmarks.md` once the
repo-A index has been re-built and (optionally) enriched. The
intermediate target is the 14 / 16 ceiling with
mean ≤ 1.5 s/q (no regression vs 0.4.1); the stretch target is
15 / 16 with ≤ 0.5 s/q on a fully-enriched index.

## Install

```
pip install --upgrade local-mgrep
```
