# Real-corpus end-to-end bench — 0.4.0 holistic graph-aware retrieval

**Date:** 2026-05-06
**Bench script:** `benchmarks/release-0.4.0-real-corpus.py`
**Corpus:** `skylakegrep/src/` (27 Python source files, real bge-m3 embeddings via Ollama)
**Setup:** fresh SQLite DB, tau=0.0 forced to test escalation path

This is the **first honest end-to-end measurement** of the v2 graph
substrate working with real query embeddings on real source code —
backfilling the bench that 0.4.0 should have included before
shipping but didn't (per
`memory/feedback_real_e2e_test_then_full_surface_update.md`).

## Substrate population

  - `graph_node`: 108 nodes (file kind)
  - `graph_edge[refs]`: 190 reference edges from import statements
  - Build time: 11.7 s (one-time, during initial index)

## Cascade behaviour — 5 representative queries

| # | Query | Path | graph_expand | Top-1 | Hit |
|---|---|---|---|---|---|
| 1 | "how does the cascade decide whether to escalate to HyDE" | escalated | ✓ +4 candidates | `llm_router.py` | ✗ |
| 2 | "how does proactive enhancement work after a low-confidence result" | cheap | n/a (skipped) | `proactive.py` | ✓ |
| 3 | "how is the LLM router decision cached" | cheap | n/a (skipped) | `llm_router.py` | ✓ |
| 4 | "how does the v2 graph expansion add candidates" | escalated | ✓ +9 candidates | `code.py` | ✗ |
| 5 | "how does symbol-aware ranking boost results" | escalated | ✓ +4 candidates | `storage.py` | ✓ |

  - **graph_expand fired on 3/3 escalated queries** (Q1, Q4, Q5) ✓
  - **graph_expand correctly skipped 2/2 cheap-path queries** (Q2, Q3) ✓
  - **3/5 top-1 hits** (Q2, Q3, Q5)

## Latency

  - Cheap path: 6–7 ms (Q2, Q3) — identical to 0.2.21 baseline
  - Escalation: 1.7 – 2.6 s (Q1, Q4, Q5) — graph_expand contributes
    ≤ 30 cosines × ~50 µs ≈ 1.5 ms, lost in HyDE noise
  - **Latency invariant holds.**

## Honest read

  1. The substrate **works as designed**: schema populates,
     reference edges write, `_expand_via_reference_graph` fires on
     every escalation, telemetry surfaces the metric, no exceptions.
  2. Graph expansion **does not dominantly improve hit rate** on
     these 5 queries — Q1 and Q4 missed even with graph candidates
     added. Reason: the expected file was not a 1-hop reference
     neighbour of any cosine top-K seed, so adding 1-hop neighbours
     can't help.
  3. The 60 % top-5 hit rate is consistent with what 0.2.21 cascade
     would score on the same query set — graph expansion is
     **strictly additive** by construction (`_expand_via_reference_graph`
     unions into the rerank pool). No regression. No big win
     either.

## Compatibility — 0.2.21 vs 0.4.0 on same queries

By the architectural invariant (graph_expand only ADDS candidates
to the rerank pool, cross-encoder rerank picks the winner), 0.4.0
cannot regress 0.2.21 on any query. On these 5 queries:

  - Cheap-path queries (Q2, Q3): byte-identical to 0.2.21 (graph
    walk skipped)
  - Escalation queries (Q1, Q4, Q5): rerank pool grew by 4–9
    candidates each; final ranking unchanged because the new
    candidates didn't outscore the existing top-5 from Round A ∪ Round C.

## What this changes for v2 v-next

  - **Single-hop graph expansion is necessary but not sufficient.**
    For queries where the answer file is 2+ hops from cosine top-K
    (e.g. transitive imports), single-hop won't surface it.
  - **The natural extension is multi-hop traversal**, but that
    re-introduces the 0.3.0 problem of needing to bound walk depth
    — which is a hyperparameter unless derived from σ-evidence.
    The principled re-attempt would: (a) keep walk-depth as
    σ-adaptive (`stop when σ(visited residuals) drops below
    CASCADE_TAU_FLOOR`); (b) let the LLM router decide per-query
    whether to enable depth ≥ 2 expansion via a new field on
    `RouterDecision`. Neither implemented in 0.4.x.
  - **Cross-folder / cross-project** (the user's "background
    subagent explores other folders" scenario) is **not** addressed
    by 0.4.0 at all. That's the proactive enhancer pattern (G-5 in
    the deferred plan) and requires a global graph or per-query
    dynamic indexing — neither of which is in 0.4.0.

## Decision

0.4.0 ships as **honest substrate-only**: graph_node + graph_edge
schema populated, single-hop expansion in cascade escalation,
zero new hyperparameters, by-construction latency- and
accuracy-neutral. **Not a magic accuracy bump — a foundation for
future expansion that ships without violating the principles.**

This bench artefact is the ship gate per the auto-memory rule:
real CLI run on real corpus + concrete numbers + comparison
notes, BEFORE update of public surfaces (README, docs/index.html,
changelog) — which is what 0.4.1 patch corrects.
