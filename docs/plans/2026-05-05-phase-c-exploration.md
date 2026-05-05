# Phase C exploration — second-pass audit

**Date:** 2026-05-05
**Author:** exploration sub-audit (parent doc:
[`2026-05-05-phase-c-audit.md`](2026-05-05-phase-c-audit.md))
**Status:** non-implementation; recommendations only.

This document is a structured rebuttal / extension of the parent audit's
**C ≻ B ≻ A** ranking, written after re-reading
`skylakegrep/src/storage.py`, `symbol_channel.py`, `recovery.py`, the
0.2.0 / 0.2.2 release notes, the 30-task public bench, and the README.

---

## 1. Validate or challenge the C ≻ B ≻ A ranking

**Verdict: directionally correct, but the absolute argument for C is
weaker than the doc claims and the case for A is dismissed too fast.**

What I agree with:

- B's "principled" framing is real. A `register_extractor()` already
  exists for the reference graph
  ([`README.md:62-69`](../../README.md)); a parallel
  `register_structural_extractor()` for retrieval channels would be
  the obvious symmetric move.
- A genuinely conflicts with the 0.2.0 content-agnostic ship narrative
  (`docs/skylakegrep-0.2.0.md:41-72`). That tension is real and
  unresolved.

Where I push back:

1. **C's upper bound is probably small enough to be invisible.** The
   cascade is *already* σ-adaptive
   (`storage.py:1035-1039`, `tau_eff = max(τ_floor, k·σ)`). The cheap
   path already early-exits ~80 % of queries
   (`README.md:184`). The escalation path already uses the union of
   Round A + Round C
   (`storage.py:1073-1117`). The 30/30 number is already at ceiling on
   this bench. C therefore doesn't have headroom to *prove itself* on
   the existing bench — at best it ties at 30/30 with lower latency,
   at worst it ties at 30/30 with the same latency. The doc
   acknowledges this ("possible upper bound on visible improvement",
   audit table row 1) but still ranks it #1. **A path that can only
   demonstrate value via a latency delta should be measured against
   latency variance, not recall.** I haven't seen the per-repo
   variance numbers; if Tokio's 21.9 s/q has a ±5 s shot-noise band, C
   may be statistically silent.

2. **A's "violates content-agnostic" argument is partly a marketing
   concern, not a technical one.** The router is already content-aware
   in subtle ways: the `_NON_CANONICAL_PATH_PATTERNS` list
   (`storage.py:47-67`) is implicitly code-biased (`/__tests__/`,
   `.development.js`, `_test.rs`). The non-canonical-path filter
   already concedes that a content-agnostic substrate plus
   content-aware *priors* is fine. Symbol-channel-as-prior is the
   same shape, one rung up.

3. **The audit underweights A's main risk: react-010 fusion regression
   is not a deal-breaker but a router-design problem.** If the router
   gates symbol-channel firing on σ_topK *and* on the symbol-channel
   producing more than N exact-name matches *and* on the
   symbol-channel result not being already in cosine top-3, the
   regression vanishes. The current `multi_channel_search`
   (`symbol_channel.py:284-371`) fuses unconditionally, which is why
   it regresses. That's a router bug, not a channel bug.

**My counter-ranking: B ≈ A > C (within latency-variance).** B because
it pays the principled-design cost cleanly; A because the symbol
channel already exists and the engineering cost is low; C last because
it's the highest-effort-to-falsify-against-noise path.

---

## 2. Path C: 3–5 specific scheduler decisions, with falsification tests

For each, the signal is computable from values already on hand in
`cascade_search` (`storage.py:975-1120`).

| # | Decision | Signal | Falsification test |
|---|---|---|---|
| **C1** | Skip HyDE on the escalation path when query length ≤ 4 tokens AND query has no English stopwords (i.e. it's symbol-shaped) — HyDE is a noise generator on `find tokio::spawn impl`. | `len(extract_query_terms(q))` and a stopword count from the existing tokeniser. | Wrong if removing HyDE on these queries drops recall on tokio-001/004/007/010 (the symbol-shaped Tokio tasks) by ≥ 1. |
| **C2** | Skip cross-encoder rerank when top-1 file-mean cosine ≥ 0.55 *and* σ_topK is below the adaptive floor — cosine is already over-confident; the reranker has no work to do. | `pairs[0][1]` from `_file_level_pairs` and `sigma` from `storage.py:1036`. | Wrong if any 30/30 query flips because the cross-encoder was the tiebreaker that pulled the canonical answer above a same-named test file. |
| **C3** | Force escalation (skip cheap path) when ripgrep prefilter returns < 3 candidate files but the file-mean cosine top-1 is in those 3 — small candidate sets *look* confident (high σ-gap) but are actually undersampled; this is the failure mode the 0.5.1 fix addressed for `crates/ai/`. | `len(candidate_paths)` + membership check on `pairs[0][0]`. | Wrong if escalation rate increases > 30 % on the 30-task bench without recall change (i.e. we paid latency for nothing). |
| **C4** | Pick HyDE model size by σ_topK — when σ is high, use `qwen2.5:1.5b` (cheap rewrite, won't matter); when σ is low, use `qwen2.5:3b` (expensive rewrite, real work). | `sigma` from the same line as C2. | Wrong if 1.5b-on-low-σ path matches 3b-on-low-σ recall on the bench (means model size doesn't matter and the schedule is theatre). |
| **C5** | Skip the symbol-boost + graph-tiebreak L2/L4 layers entirely on the cheap path — they only matter when scores are tied, and the cheap path already bypasses ties by definition (high σ-gap). | `early_exit` boolean. | Wrong if disabling them on the cheap path drops *any* of the 30 bench tasks. |

C5 is the cheapest test (~30 min). C3 is the most likely to find a real
bug (the parent audit's "Tokio +57 % latency regression" hint suggests
under-sampling). C1 + C2 are the highest-leverage if they hold.

---

## 3. Did the audit miss a 4th candidate path?

**Yes — three, in descending leverage:**

### Path D — Indexing-time enrichment (re-prioritise `enrich`)

`skygrep enrich` (doc2query, mentioned in `README.md:344`) runs
opt-in. The L3 enrichment columns
(`storage.py:144-148`,
`description TEXT, enriched_at REAL`) are already wired through. The
parent audit's MEMORY note records that the Rust workspace
`crates/ai/` and `app/src/billing/` cases are still open hard misses
with the cascade *and* the 0.5.1 escalation widening. Those are
exactly the cases doc2query was designed to fix.

**Why it's better than A/B/C for the residual misses:** unlike
symbol-channel (A) it's content-agnostic; unlike B it's already shipped;
unlike C it actually has measurable headroom (the two cases the cascade
doesn't solve). Cost: index-time wall-clock, not query-time latency —
which is the latency-budget direction Phase C cares about.

### Path E — Cross-document reference-graph traversal at query time

The reference graph is built (`reference_graph.py` + extractors) but
its query-time use is limited to the L4 PageRank tiebreaker
(`storage.py:651-705`). What's missing: for a query that returns
file F as top-1 with low confidence, traverse one hop in the
reference graph and include F's referrers / referents in the rerank
pool. This is content-agnostic by construction (the graph is content-
agnostic in 0.2.0) and gives the cross-encoder a chance to surface
the canonical implementation when the cosine top-K only had its
re-export aggregator. The `react-010` and `react-007` failures the
release notes call out are exactly this shape.

### Path F — Chunking-layer fix: variable-length chunks

The current chunker is tree-sitter-aware for code and line-window
otherwise. A small `crates/ai/lib.rs` with one canonical `pub fn`
gets one short chunk that competes against 50 chunks of consumer
code. File-mean cosine is the partial fix (`storage.py:290-336`) but
it averages over chunk count, not over chunk length. **A length-
weighted file mean is a one-line change** that may close the
`crates/ai/` gap without any new channel.

---

## 4. Bench expansion needed per path

Concrete suggestions. Bench file format follows
[`benchmarks/cross_repo/django.json`](../../benchmarks/cross_repo/django.json).

### Path A (symbol channel)

15 new tasks across the three repos: 5 pure-symbol, 5 pure-NL, 5 mixed.

- **Pure-symbol** (channel must win): `find createElement implementation`
  → `packages/react/src/jsx/ReactJSXElement.js`;
  `where is tokio::spawn defined` → `tokio/src/task/spawn.rs`;
  `find URLResolver class` → `django/urls/resolvers.py`.
- **Pure-NL** (channel must NOT fire — falsifiability guard):
  paraphrase the existing 30. The router that fires on these is bugged.
- **Mixed** (router must choose correctly): `where does tokio implement
  spawn for a runtime handle` — both symbol token (`spawn`) and NL
  vocab-mismatch present.

### Path B (structural-channel registry)

Beyond A's 15, add 12 markdown + 6 PDF tasks against an actual
markdown / PDF corpus. The skylakegrep repo's own `docs/` directory
has ~30 markdown files and is suitable. Sample:
`where is the σ-adaptive cascade explained` →
`docs/skylakegrep-0.2.0.md`. Without a non-code corpus B is unfalsifiable.

### Path C (smarter cascade)

**No new tasks needed for recall.** Use the existing 30 + a latency-
variance protocol: 5 runs per task, report median + IQR. C only earns
its keep on latency, so the bench needs latency error bars it currently
doesn't have. The audit ranks C #1 partly *because* its bench coverage
"already exists" — but the existing bench has no variance estimate, so
that claim is partially false.

---

## 5. Failure modes the parent audit hasn't covered

1. **Recovery worker × Phase C router race.** During recovery
   (`recovery.py:231-323`), some chunks have stale-dim vectors that
   `_filter_to_matching_dim` strips. If Phase C's router decides on
   σ_topK *before* the filter runs, the σ statistic is computed over a
   smaller-than-expected pool. Both σ-cascade and any new channel
   that fires on σ-evidence will see different signals during
   recovery vs after. The 0.2.2 `quality=DEGRADED-recovery` tag is
   the right surface — but Phase C must be aware that its scheduler
   inputs are themselves unreliable during recovery.

2. **σ-cascade gap × symbol-channel fusion.** RRF fuses by rank, not
   score (`symbol_channel.py:236-281`). If the cascade's σ is computed
   on cosine scores but the final ranking is RRF-fused, the σ-gap
   loses meaning — the rank distribution of the fused list has
   different statistics. Phase C / Path A must either (a) recompute
   σ on fused scores, or (b) gate fusion on the cascade decision so
   the two layers are sequential, not interleaved.

3. **Latency creep from telemetry.** The 0.2.2 footer reads recovery
   state on every render (`recovery.py:131-159`). Adding more
   telemetry fields (path-taken, σ-evidence, channel-firings) compounds.
   On the cheap path the budget is < 200 ms warm — three SQL reads at
   ~3 ms each is 5 % of that. Phase C should batch metadata reads.

4. **`_NON_CANONICAL_PATH_PATTERNS` list staleness.** This is a
   handwritten list of 24 patterns (`storage.py:47-67`). If the
   structural-channel registry (Path B) infers content-type-specific
   "non-canonical" priors per registered extractor, the global list
   should be deprecated in favour of per-extractor priors. Otherwise
   Path B + the global list double-penalise.

5. **`tau_static` vs `tau_eff` confusion in user-tunable surface.**
   The user-facing `--cascade-tau` (`README.md:295`, default 0.015)
   *only* applies in static mode; once `CASCADE_K_SIGMA > 0`
   (the default) it's a floor that's silently overridden by the
   adaptive scale (`storage.py:1037`). Any Phase C scheduler that
   adds more tunables risks compounding this surprise.

6. **Daemon mode (`skygrep serve`) caching invariants.** Path C's
   per-query scheduler decisions must be deterministic given (query,
   index-state). Stochastic decisions (e.g. random sampling for σ
   estimation) would break daemon-mode caching subtly.

---

## 6. Recommended next concrete step (≤ 4 h)

**Run a latency-variance ablation on the existing 30 tasks before
choosing any path.**

Specifically:

```
for each of the 30 tasks:
    for each cascade variant in [
        baseline,
        no-cross-encoder,            # tests C2
        no-symbol-boost-on-cheap,    # tests C5
        no-HyDE-on-symbol-shaped-q,  # tests C1
    ]:
        run 5 times, record latency, record top-10 paths
report median latency, IQR, recall
```

This produces three signals in one afternoon:

- **The latency-variance band per repo.** Without this, none of A/B/C
  can be empirically distinguished from noise.
- **Whether C1/C2/C5 individually move recall.** If any drops recall
  even once on the 30-task bench, that scheduler decision dies on
  the spot — falsification before commitment.
- **A baseline against which to measure Phase C / Path A / Path B
  in subsequent work.** Current numbers are point estimates; this
  gives them error bars.

If C5 (skip L2/L4 on cheap path) is recall-neutral and saves > 50 ms,
ship it under the 0.2.x banner without picking a Phase C direction.
That's the cheapest "intelligence" win on the table and it doesn't
require committing to A/B/C at all.

---

## tl;dr

The parent audit's **C ≻ B ≻ A** ranking is directionally fine but
overrates C — C can only prove itself via a latency delta the existing
bench has no variance estimate for. A is dismissed too quickly: the
react-010 regression is a router-design problem, not a channel
problem. The audit also misses three viable 4th paths — promoting
`skygrep enrich` (Path D, content-agnostic, already shipped, fixes
the residual `crates/ai/` misses), one-hop reference-graph
traversal at query time (Path E), and a length-weighted file-mean
cosine (Path F, one-line). Before picking, spend one afternoon on a
5-run latency-variance ablation of the 30 tasks; the resulting error
bars determine whether C is even falsifiable. The smallest immediate
win is probably C5: skip L2/L4 on the cheap-path early-exit branch
where the σ-gap already settled the ranking.
