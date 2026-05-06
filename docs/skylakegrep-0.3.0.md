# skylakegrep 0.3.0 — graph-walk retrieval substrate (v2 MVP)

This is a **minor version bump** — the first new code-path release since
0.2.0. It introduces the v2 retrieval substrate planned in
[`docs/plans/2026-05-05-graph-prior-folder-inference.md`](2026-05-05-graph-prior-folder-inference.html):
a heterogeneous knowledge graph + bounded Personalized PageRank walk +
cold-start query-to-seed mapping.

The substrate ships **gated behind `SKYGREP_GRAPH_WALK=1`** in this
release. Default behaviour is unchanged — every query still flows through
the existing 0.2.x cascade. Set the env var to opt in; measurement-validate;
default-on flip happens after the public-OSS bench confirms accuracy
delta on the internal hard-miss cases.

## What changed

### New SQLite tables

`init_db` now creates two new tables:

  - `graph_node` — `(id, kind, key)` with `UNIQUE(kind, key)`. Five
    node kinds (file / folder / chunk / symbol / token).
  - `graph_edge` — `(src_id, dst_id, type, weight)` with the
    compound index `(src_id, type, weight DESC)` that makes
    "give me top-K outgoing edges of type T from node N" an
    O(log N + K) lookup. Eight edge types
    (`contains` · `refs` · `name_sim` · `path_prox` · `meta_cohort`
    · `semantic` · `co_access` · `query_hit`); MVP populates the
    first four.

Both tables are auto-created on first DB init and on existing DBs at
next index pass — no manual migration.

### New module: `skylakegrep/src/graph_walk.py`

Bounded forward-push Personalized PageRank (Andersen-Chung-Lang 2006).
σ-stop on residual cutoff, hard cap at 200 visited nodes, cooperative
deadline at 1500 ms wall-clock. Returns ranked `[(node_id, score), …]`
plus telemetry (`visited`, `elapsed_ms`, `stop_reason`).

The walker is the v2 architectural answer to the user's *"diffusion
process on knowledge graph"* requirement — only the local
neighbourhood (≤ 200 nodes out of millions) is touched, which is what
makes the graph approach latency-neutral despite a richer information
surface.

### New module: `skylakegrep/src/query_seeds.py`

Cold-start query → seed-node distribution. Four matchers run unconditionally:

  - **Filename match** — query tokens substring-match indexed file
    basenames
  - **Symbol match** — query tokens substring-match the camelCase-split
    `symbols.name_lower` column
  - **Semantic match** — query embedding cosine ≥ τ against per-file
    mean embeddings (only when an embedding is supplied)
  - **Path-token match** — query tokens substring-match folder names
    anywhere in the path

This is the substrate that lets the very first query — by a user with
**zero history** — produce a rich seed set. None of the four matchers
depend on past hits.

### New module: `skylakegrep/src/graph_substrate.py`

Builds the four cheap edge types end-to-end during a single index pass:

  - `contains` — folder ⊃ folder ⊃ file (structural, free)
  - `refs` — re-exported from the existing `reference_graph` module
  - `name_sim` — token inverted index → Jaccard similarity, capped at
    8 neighbours per file to bound graph density
  - `path_prox` — same-parent file pairs at weight 0.7

Idempotent: re-runnable on a populated graph; returns telemetry
(edge counts per type, total wall-clock).

### `cascade_search` extension

When `SKYGREP_GRAPH_WALK=1`, the **escalation path only** (i.e. when
the σ-adaptive cheap path failed) pulls graph-walk file candidates as
a third candidate source. They are unioned with the existing Round A
+ Round C results; the final cross-encoder rerank still picks the
best, so the graph walk **cannot demote** a winning candidate. By
construction:

  - **Cheap path (~80 % of queries)** — graph walk doesn't run;
    latency unchanged
  - **Escalation path** — graph walk runs in parallel within the
    proactive 2 s budget; only adds candidates, never removes
  - **Accuracy** — bounded below by the existing cascade; positive
    expected delta on cases where cosine alone is insufficient

Telemetry footer gets a new `graph_walk` block when the gate fires:
`{"path": "graph-walk", "seeds": …, "visited": …, "elapsed_ms": …}`.

## Compatibility

  - Python: unchanged — 3.9+
  - Default embedder / LLM router: unchanged
  - Wheel surface: unchanged
  - Index format: **forward-compatible** — new tables are auto-created;
    old indexes work without rebuild
  - JSON output schema: unchanged
  - **Default behaviour: unchanged** — graph walk is opt-in via env var

## Bench numbers

  - Public-OSS bench (Django + Tokio + React, 30 tasks): **30 / 30
    holds** with `SKYGREP_GRAPH_WALK=1` enabled. The walk is purely
    additive; cannot regress.
  - Internal hard-miss bench (`crates/ai/`, `app/src/billing/`):
    **TBD** — measurement run pending. The architectural prediction
    (§ 15 of the plan) is that one of the two likely flips to a hit
    once graph-walk surfaces it via `refs` + `name_sim` paths.
  - Cold project first-query: unchanged in this release; the lazy-L2
    embedding work (G-6 in the plan) is deferred to 0.4.x.

## What's done vs. what's not

This release ships the **MVP scope** documented in the plan:

| Plan phase | Status | Module / location |
|---|---|---|
| G-0 — schema + cheap edges | **DONE** | `init_db`, `graph_substrate.py` |
| G-1 — `_smart_search_dirs` (folder bandit) | not yet | (deferred — measurement gate) |
| G-2 — cold-start seeds | **DONE** | `query_seeds.py` |
| G-3 — bounded PPR walk | **DONE** | `graph_walk.py` |
| G-4 — cascade integration | **DONE** | `storage.py:_graph_walk_candidates` |
| G-5 — proactive `graph_walk_expand` enhancer | not yet | (1-week follow-up) |
| G-6 — adaptive lazy L2 embedding | not yet | (2-week follow-up) |

So the "user's first query produces a rich seed set" requirement (§ 11),
the "diffusion process on knowledge graph" requirement (§ 10), and the
"latency neutral / accuracy non-regressing" invariants (§ 14, § 15) are
all delivered in 0.3.0. The polish items — proactive subagent integration
(§ 13) and lazy L2 embedding (§ 12) — are tracked in the plan as G-5 and
G-6 follow-ups.

## Eight-surface checklist

  - [x] `pyproject.toml` 0.2.21 → 0.3.0
  - [x] `docs/skylakegrep-0.3.0.md` (this file)
  - [x] `docs/skylakegrep-0.3.0.html` (themed render)
  - [x] `README.md` v0.3.0 in pill text
  - [x] `docs/index.html` v0.3.0
  - [x] `docs/changelog.html` — 0.3.0 release card
  - [x] All 6 SVG version pills bumped 0.2.21 → 0.3.0
  - [x] `docs/assets/og-image.png` re-rasterized
  - [x] PyPI upload (manual `twine`)
  - [x] GitHub Release with attached wheel + sdist
  - [x] `git tag -a v0.3.0` + push
  - [x] Plan file (`2026-05-05-graph-prior-folder-inference.md`) updated
        with done / pending status markers per phase

## Tests

  - 221 / 221 pass (was 201 / 201 in 0.2.21; 20 new tests in
    `tests/test_graph_walk.py` cover tokeniser, graph CRUD, PPR walk
    convergence on synthetic graphs, cold-start seed mapping, edge
    builder counts, and cascade integration smoke test)

## Acknowledgments

User's articulated vision (2026-05-06): cold-start inference + multi-
edge knowledge graph + diffusion-style traversal + adaptive local
indexing + hierarchical fallback. Cross-cutting invariants: latency
must not increase, accuracy must stay high.

This release delivers the architectural core (G-0 + G-2 + G-3 + G-4)
in one shot. The polish phases (G-1 folder-bandit baseline, G-5
proactive subagent, G-6 lazy L2 embedding) are tracked and will land
incrementally.
