# Intelligent local search system — v0.5.0 execution plan

Status: **planning → execution starts 2026-05-03**.
Target release: v0.5.0 (potentially split into 0.5.0 + 0.6.0 if doc2query
takes longer than budgeted).

This is the concrete implementation plan for the "5-layer progressive
enhancement" architecture decided in conversation on 2026-05-03. Every
layer is offline-paid and query-time-free — the goal is **sub-300 ms
queries with 16/16 repo-A recall** on a fully-enriched index.

The plan covers three new layers on top of the 0.4.1 base:

  - **L2 — symbol-aware indexing**: tree-sitter extracts function /
    struct / class / impl / module names per chunk; query-time exact
    match adds a multiplicative boost. Attacks the "concept word lives
    in the symbol name, not the body" failure mode. Estimate: 3 days.

  - **L3 — doc2query chunk enrichment**: background LLM pass writes a
    1-2 sentence natural-language description of each chunk, the
    description is appended to the chunk text and the chunk is
    re-embedded. Eliminates the need for query-time HyDE. Estimate:
    ~1 week (resumable; can ship 0.5.0 with the harness in place
    and let the enrichment finish for individual users in their own
    indexes over hours).

  - **L4 — file-export PageRank tiebreaker**: walk all source files
    once, build a use/import graph, run PageRank, store the score
    per file. Used at search time only when top-1 and top-2 cosine
    scores are within ε; the higher-PageRank file wins. Estimate:
    2 days.

The 0.4.1 layers (L0 ripgrep fallback, L1 chunk + file-mean cosine
cascade) stay exactly as they are — this plan is purely additive.

---

## 1. Layered architecture (the design)

```
┌──────────────────────────────────────────────────────┐
│ user query                                           │
└──────────────────────────────────────────────────────┘
                    ▼
┌──────────────────────────────────────────────────────┐
│ L0  ripgrep fallback        — 0.4.1, ~500 ms         │
│     (always available, no setup)                     │
└──────────────────────────────────────────────────────┘
                    ▼ (when index ready)
┌──────────────────────────────────────────────────────┐
│ L1  chunk + file-mean cosine + cascade — 0.4.0       │
│     ~150 ms                                          │
└──────────────────────────────────────────────────────┘
                    ▼ (when L2 ready)
┌──────────────────────────────────────────────────────┐
│ L2  symbol exact-match boost — 0.5.0                 │
│     ~5 ms increment                                  │
└──────────────────────────────────────────────────────┘
                    ▼ (always when present)
┌──────────────────────────────────────────────────────┐
│ L3  doc2query enriched embeddings — 0.5.0/0.6.0      │
│     embedding already absorbs LLM description;       │
│     no incremental query cost                        │
└──────────────────────────────────────────────────────┘
                    ▼ (only on tied candidates)
┌──────────────────────────────────────────────────────┐
│ L4  file-export PageRank tiebreaker — 0.5.0          │
│     ~0 ms (table lookup)                             │
└──────────────────────────────────────────────────────┘
                    ▼
                Top-K results
```

Each layer is **independent and resumable**. A query against a project
where only L0 and L1 are ready returns L1 results immediately; the
status line names the highest layer used (e.g.
`[0.18s · cosine · L2 building 12% files]`).

## 2. L2 — Symbol-aware indexing (3 days)

### Schema changes

```sql
CREATE TABLE IF NOT EXISTS symbols (
    file       TEXT NOT NULL,
    name       TEXT NOT NULL,
    name_lower TEXT NOT NULL,
    kind       TEXT NOT NULL,    -- function/struct/class/module/trait/impl
    start_line INTEGER,
    end_line   INTEGER,
    file_mtime REAL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name_lower ON symbols(name_lower);
CREATE INDEX IF NOT EXISTS idx_symbols_file       ON symbols(file);
```

### Producer (index time)

`local_mgrep/src/indexer.py` extends `prepare_file_chunks` (or a new
``extract_file_symbols`` peer) to walk the tree-sitter AST and emit
symbol rows. CamelCase identifiers get split into space-separated
tokens for the lower-cased index column (so the query "language model"
matches symbol `LanguageModelClient`).

### Consumer (query time)

`local_mgrep/src/storage.py` adds a `symbol_match_boost(conn,
query_text, candidate_paths)` helper. For each candidate file it
computes:

```
boost(file) = (matched_symbols / max(1, len(query_terms))) * SYMBOL_WEIGHT
```

where `matched_symbols` counts distinct query terms (≥4 chars) that
exact-match (case-insensitive) any tokenised symbol name in `file`.

`SYMBOL_WEIGHT` defaults to `0.10` (additive bonus on top of the
final score after rerank/penalty/file-rank). Tunable via env
`MGREP_SYMBOL_WEIGHT`.

### Migration

Indexes built before 0.5.0 do not have a populated `symbols` table.
On first query against an old index, we silently call
`populate_symbols(conn, root)` once (it's pure tree-sitter, no LLM,
typically < 5 s for ~5 K files) and persist the result. No reindex
required.

### Tests

- `tests/test_symbol_index.py` covers extraction, lowercase token
  splits, exact-match boost on a synthetic 3-file project.

## 3. L3 — doc2query chunk enrichment (~1 week)

### Schema changes

```sql
ALTER TABLE chunks ADD COLUMN enriched_at  REAL;
ALTER TABLE chunks ADD COLUMN description  TEXT;
```

### Worker (background, resumable)

New module `local_mgrep/src/enrich.py` with:

```python
def enrich_pending_chunks(
    conn,
    *,
    embedder=None,
    answerer=None,
    batch_size: int = 5,
    max_chunks: int | None = None,
    quiet: bool = False,
) -> int:
    """Pop chunks where enriched_at IS NULL, ask the LLM for a 1-2
    sentence description, write it to ``description``, recompute the
    embedding over (chunk_text + description), update the row,
    commit. Stop after ``max_chunks`` (or run to completion)."""
```

Prompt template (deterministic seed):

```
Write a one or two sentence high-level description of what this code
does, focusing on user-facing concepts (e.g. "auth", "billing",
"language model backend") rather than implementation detail. Output
only the description, no preamble, no markdown.

File: {path}
Symbol: {symbol_name_or_blank}

```{language}
{chunk}
```
```

### CLI

New top-level command:

```
mgrep enrich [PATH] [--max N] [--batch B]
```

Synchronous path; useful for users who want to wait. The bare-form
search path also auto-triggers `enrich_pending_chunks` in a separate
detached process when L1 is fully ready and ``MGREP_AUTO_ENRICH`` is
not set to ``no``.

### Query-time consumer

No change. The enriched chunks already carry the description in their
embedded text, so cascade and chunk cosine pick them up automatically.

### Resumability

`enriched_at` is the resume primitive. Crashed enrichment workers
resume from the next chunk with `enriched_at IS NULL`. The lockfile
pattern from 0.4.1's background indexer carries over (`<db>.enrich.lock`
mirrors `<db>.lock`).

### Tests

- `tests/test_enrich.py` mocks the LLM to a deterministic
  `f"description for {file}"` and asserts schema + resume semantics.

## 4. L4 — File-export PageRank tiebreaker (2 days)

### New module

`local_mgrep/src/code_graph.py`:

```python
def build_export_graph(root: Path) -> dict[str, dict[str, float]]:
    """Walk source files; for each file compute (in_degree,
    out_degree, pagerank). Return {file_path: {indeg, outdeg, pr}}."""

def populate_graph_table(conn, root: Path) -> int: ...
```

Use the existing per-language regex parsers from
`benchmarks/code_graph_probe.py` (they were validated in P4). PageRank
from a hand-rolled iteration (no external deps) so we don't pull
NetworkX.

### Schema

```sql
CREATE TABLE IF NOT EXISTS file_graph (
    file       TEXT PRIMARY KEY,
    in_degree  INTEGER,
    out_degree INTEGER,
    pagerank   REAL,
    file_mtime REAL
);
```

### Consumer (query time)

In `storage.search`, after the final scoring pass and before
`diversify_results`/`_file_rank`, examine the top-2 entries:

```
if score[0] - score[1] < TIEBREAK_EPS:        # default 0.005
    apply_pagerank_tiebreaker(top_K=5)        # only the head, never the tail
```

`apply_pagerank_tiebreaker` reads `file_graph.pagerank` for each of
the top-K paths and breaks ties via:

```
score'(c) = score(c) + GRAPH_TIEBREAK_WEIGHT * normalized_pagerank(c)
```

where `GRAPH_TIEBREAK_WEIGHT` defaults to `0.005` (≤ epsilon, so it
only changes orderings that were near-ties to begin with). Tunable
via `MGREP_GRAPH_TIEBREAK_WEIGHT`.

### Migration

Indexes built pre-0.5.0 trigger a one-time `populate_graph_table` on
first query (similar pattern to L2). Pure regex + matrix iteration —
no LLM.

### Tests

- `tests/test_code_graph.py` builds a small 4-file synthetic graph,
  asserts in-degree counts and that PageRank is monotonic in
  in-degree on the simple case.

## 5. Integration plan & multi-agent execution

The three layers above are **mostly orthogonal**:

  - L2 changes: `storage.py` (new schema + helpers), `indexer.py`
    (symbol extraction in chunk prep), `cli.py` (search status
    line gains "L2 ready"), tests.
  - L3 changes: new `enrich.py` module, schema columns on `chunks`,
    `cli.py` (new `enrich` command, auto-trigger hook), tests.
  - L4 changes: new `code_graph.py` module, new `file_graph` table,
    `storage.py` `search()` tiebreaker, tests.

**Shared touch points**: `storage.py` schema extensions, `cli.py`
status-line text. Conflicts here are mechanical merges.

**Multi-agent layout**:

  - Worktree A (`local-mgrep-L2`): branch `feature/symbol-index`,
    agent implements L2.
  - Worktree B (`local-mgrep-L3`): branch `feature/doc2query`,
    agent implements L3.
  - Worktree C (`local-mgrep-L4`): branch `feature/code-graph`,
    agent implements L4.

After all three report 'tests pass', the integrator (main session)
merges A → B → C onto a release branch, runs the full repo-A benchmark,
adjusts weights if needed, and releases as 0.5.0.

## 6. Status-line evolution

The status line gains additional segments to make the layered system
visible to the user:

```
[0.18s · cascade=cheap (gap=0.024 τ=0.015) · index 12 min ago · 3247 files]                       (0.4.1)
[0.20s · cosine+symbol · 3247 files · L3 enriching 12%]                                           (0.5.0 mid-build)
[0.20s · cosine+symbol+enriched · 3247 files · graph prior 0.012]                                 (0.5.0 fully built)
```

## 7. Benchmarks & exit criteria

Before tagging 0.5.0:

  - 24 / 24 unit tests pass.
  - `benchmarks/parity_vs_ripgrep.py --tasks benchmarks/cross_repo/repo-a.json`
    shows recall ≥ 14 / 16 with mean latency ≤ 1.5 s/q (i.e. no
    regression vs 0.4.1).
  - The two hard misses (`crates/ai/`, `app/src/billing/`) attempt
    list shows whether L2 and L3 each improve them; documented in
    `docs/parity-benchmarks.md` even when the lift is null.
  - `mgrep doctor` reports L2 / L3 / L4 readiness.

Stretch (would push to 0.6.0 instead of 0.5.0):

  - Full doc2query enrichment of a fresh repo-A index measured on the
    16-task benchmark; target 15 / 16 recall with mean latency
    ≤ 0.5 s/q (no LLM call at query time).

---

This plan lives at `docs/plans/2026-05-03-intelligent-system-v0.5.md`
and is referenced from `docs/roadmap.md` (P5 / P6 / P7 sections).
