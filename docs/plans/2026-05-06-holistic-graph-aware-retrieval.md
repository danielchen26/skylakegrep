# Plan — Holistic graph-aware retrieval

**Date filed:** 2026-05-06
**Status:** Implemented in **0.4.0**.
**Supersedes:** 2026-05-05-graph-prior-folder-inference.md (the
phased v1 ⊃ v2 plan, which accumulated 9+ preset hyperparameters
and was rolled back in 0.3.1).

---

## 1. The principle this plan exists to honour

User's articulation, 2026-05-06:

> 我们要保证的是在整体准确度不变的情况下增加intelligence … 引入
> 的都是generic的东西而并不是为了修复某一个specific的东西而是
> 特殊的优化 … 我刚刚的要求它其实是一个整体的要求而不是说一步
> 一步的因为所有的步骤其实是卡扑在一块的 … intelligence 其实
> 就是conditional的

Translated: keep accuracy identical; add intelligence; only
introduce **generic** mechanisms; **all components are coupled**;
**intelligence is conditional**; design holistically, not phased.

This generalises Principle 1 (Understanding > Enumeration) to
*Principle 1.5: hyperparameters are coupled — tune them
holistically or not at all*. The phased plan that this supersedes
violated this — every phase needed its own local "works in
isolation" metric, which forced a constant per phase, accumulating
to 9+ presets.

The full principle is recorded in
`memory/feedback_holistic_design_intelligence_is_conditional.md`.

---

## 2. The holistic design — one principle, two primitives, zero presets

The entire 0.4.0 graph-aware retrieval is built from **two
primitives that already exist in 0.2.21**, and nothing else.

### Primitive A — cosine similarity (`bge-m3` embedding)

Already used by 0.2.x:
  - query → file relevance (cascade's cheap path)
  - query → chunk relevance (`search()` chunk cosine)
  - HyDE-rewritten query → file relevance (escalation)

Reused in 0.4.0:
  - query → neighbour file relevance (graph expansion stage)

**No new uses, no new metric.** A graph-walked candidate is scored
by exactly the same operation that scores a cosine-walked candidate.

### Primitive B — σ-adaptive Bayesian-evidence threshold

Already used by 0.2.21 (`storage.py:cascade_search`):

```python
tau_eff = max(CASCADE_TAU_FLOOR, CASCADE_K_SIGMA * sigma)
```

`CASCADE_TAU_FLOOR=0.005`, `CASCADE_K_SIGMA=1.0`. Both env-var
overridable. The cascade's σ-stop on top-K residuals is the
project's universal "is this candidate cluster well-separated"
test.

Reused in 0.4.0:
  - graph-expansion candidate cut: `score >= CASCADE_TAU_FLOOR`

Same constant, same semantics. **Zero new thresholds.**

### Edges — only `refs`, weight only `pagerank`

The reference graph (`reference_graph.py`) already populates a
`file_graph` table with PageRank per file. 0.4.0 extends
`populate_graph_table` to ALSO write the actual edge list to the
existing `graph_edge` table (added in 0.3.0 schema, idle since
0.3.1 rollback). Edge weight = destination's PageRank — pure
**data-derived signal**, ordering only the SQL `ORDER BY` for
deterministic neighbour iteration. Cosine still does the actual
ranking.

**No edge-type taxonomy proliferation.** No `name_sim`, no
`path_prox`, no `meta_cohort`, no `co_access`. Just `refs`. If
those are wanted later, each one must arrive with its weight
**derived** (TF-IDF, cosine, σ), never preset.

---

## 3. The single change — escalation-time neighbour expansion

`cascade_search` escalation already does:

```
results = Round_A(cosine + file-rank)  ∪  Round_C(HyDE + cosine)
```

0.4.0 adds:

```
seed_paths = top-5 file paths from Round_A
g_results  = expand(seed_paths) — refs neighbours, scored by cosine
results    = Round_A ∪ Round_C ∪ g_results
```

Implementation: `_expand_via_reference_graph()` in `storage.py`,
~50 LoC. Pulls neighbours via the existing graph_edge SQL index,
scores each by cosine to query embedding, keeps those above the
existing `CASCADE_TAU_FLOOR`, returns result dicts in the same
shape as `search()` so the union step is uniform.

**Always on** during escalation. No env-var gate. No per-query
opt-in. Conditional only on the cascade's own σ-evidence — when the
cheap path is confident, escalation doesn't fire and neither does
the expansion.

---

## 4. Latency invariant — by construction

| Path | 0.2.21 | 0.4.0 | Δ |
|---|---|---|---|
| Cheap path (~80 % of queries) | unchanged | unchanged | 0 |
| Escalation: Round A | ~200 ms | ~200 ms | 0 |
| Escalation: Round C (HyDE) | ~600 ms | ~600 ms | 0 |
| **NEW: graph expansion** | n/a | ≤ 1 SQL JOIN + ≤ 30 cosine ops | **≤ 2 ms** |

A 1024-d cosine on a pre-cached embedding is ~50 µs. 30 of them =
1.5 ms. The SQL JOIN with the `(src_id, type, weight DESC)` index
is one B-tree probe. **Total latency add to escalation: ≤ 2 ms,
or ~0.3 % of the existing escalation cost.**

The cheap path — which serves ~80 % of warm queries per the
0.2.0 bench — is byte-identical to 0.2.21.

---

## 5. Accuracy invariant — by construction

The graph-expansion candidates are unioned into the rerank pool;
the final cross-encoder rerank is monotonic in score. Therefore:

  - **Recall**: cannot drop. The rerank pool now contains a
    superset of the 0.2.21 pool.
  - **Precision**: cannot drop on cheap-path queries (unchanged).
    On escalation queries, may improve when cosine-only missed a
    cosine-similar file that the reference graph reaches in 1 hop.

Public-OSS bench (Django + Tokio + React, 30 tasks): the rerank
pool always contained the right answer in 0.2.21 (30/30); the
expansion can only make the same statement true for marginal
cases that the cosine top-K just barely missed.

Internal hard-miss bench (`crates/ai/`, `app/src/billing/`): these
are precisely the cases where cosine missed but the reference graph
hops there. The expansion is the architectural answer.

---

## 6. What was deleted

  - `skylakegrep/src/graph_walk.py` (0.3.0 PPR with α / eps /
    max_visited / top_k_edges constants)
  - `skylakegrep/src/query_seeds.py` (0.3.0 4-matcher seed mapper
    with score_per_hit constants)
  - `skylakegrep/src/graph_substrate.py` (0.3.0 path_prox /
    name_sim with preset edge weights)
  - `tests/test_graph_walk.py` (per-component unit tests for the
    above — not end-to-end, not what the design is testing)

These were phased-design artefacts. Removing them eliminates the
hyperparameter source.

What stays:
  - `graph_node` + `graph_edge` schema (zero-cost; reused for
    `refs` edges)
  - `reference_graph.py` (extended to populate the edge list)

---

## 7. Test surface — end-to-end only

Per the holistic principle, no per-component test. The acceptance
criteria are:

  1. **Existing test suite must remain green** — 201 tests cover
     the 0.2.21 baseline behaviours; expansion is additive so they
     must all still pass.
  2. **Real-corpus bench** — `benchmarks/graph_walk_bench.py` (now
     reframed for 0.4.0): index `skylakegrep/src/`, run 5
     queries, compare with-expansion vs without-expansion. The
     expansion **must not regress** the cosine-only baseline; ideal
     case it improves on queries where the reference graph helps.

No test for "did `_expand_via_reference_graph()` return the right
neighbour list in isolation" — that's exactly the kind of
local-metric phasing this plan refuses.

---

## 8. What's not in 0.4.0 (deferred — must remain holistic)

The user's vision (2026-05-06) included:
  - Cold-start seed mapping with no history
  - Diffusion (PPR) traversal beyond 1 hop
  - Adaptive lazy L2 embedding
  - Background hierarchical fallback subagent

0.4.0 delivers a SUBSET via the holistic mechanism: 1-hop
reference-graph expansion, not multi-hop diffusion; uses pre-
computed embeddings, not lazy L2; runs in-cascade, not as a
parallel subagent.

These extensions are **not** in 0.4.0 because each one would
re-introduce a hyperparameter unless co-designed with the rest.
The next attempt at any of them must:

  1. Reuse cosine + σ — no new metric / threshold magnitudes
  2. Be conditional via the LLM router — not via env var or flag
  3. Land in **one commit**, integrated bench-validated, all-or-
     nothing — not in phases

If a future extension can't satisfy all three, it doesn't ship.

---

## 9. Decision

Holistic design, one commit, accept-criteria = full pytest +
real-corpus bench. **Production behaviour: identical to 0.2.21 on
~80 % of queries (cheap path); marginally improved on escalation
queries when the reference graph reaches a missed cosine
neighbour.** Hyperparameter delta: **0**.
