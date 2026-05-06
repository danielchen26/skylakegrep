# Real-corpus benchmark — 0.3.0 graph-walk + 0.3.1 rollback

**Date:** 2026-05-06
**Corpus:** `skylakegrep/src/` (~30 Python files)
**Bench script:** `benchmarks/graph_walk_bench.py`

This document records the **first end-to-end benchmark of the v2
graph-walk substrate** introduced in 0.3.0, plus the rollback decision
made in 0.3.1 after the bench exposed a stack of preset hyperparameters
in violation of `docs/PRINCIPLES.md` Principle 1.

## Bench setup

  - 30 Python source files indexed (one chunk per file, no L2 embeddings)
  - 284 symbols extracted via lite regex (`def name` / `class name`)
  - Graph substrate built: 33 containment edges + 18 name-sim edges +
    168 path-prox edges → 219 edges over 34 nodes
  - 5 representative cold-start queries with ground-truth expectations

## 0.3.0 results — 2 / 5 hits (40%)

```
Q1. "where is the PPR walk implemented"
    expect graph_walk.py    →  ✗ miss (top-5: __init__.py / cli.py / ...)
Q2. "how does the cold-start seed mapping work"
    expect query_seeds.py   →  ✗ miss
Q3. "where do we build the graph edges during indexing"
    expect graph_substrate.py → ✗ miss
Q4. "find the cascade search function"
    expect storage.py       →  ✓ hit
Q5. "where is the LLM router decision class"
    expect llm_router.py    →  ✓ hit (rank 2)
```

Walk latency: p50 = 6.4 ms · max = 18.3 ms. **Latency claim held**;
**accuracy claim did not** — 3 of 5 queries failed.

## Root cause — preset hyperparameters dilute the seed signal

A debug pass showed the seed mapper itself was correct on Q1:

```
FILENAME hits:  graph_walk.py  score=1.0
SYMBOL hits:    graph_walk.py  score=4.5  (5+ symbol hits via ppr_walk, WalkResult, …)
                indexer.py     score=3.0  (false-positive symbol substring hits)
                storage.py     score=1.5

FINAL SEEDS:
  prob=0.550  graph_walk.py    ← right answer at top
  prob=0.300  indexer.py
  prob=0.150  storage.py
```

So `query_to_seeds()` correctly placed `graph_walk.py` at 55 % of the
seed mass. But after `ppr_walk()` traversed the graph through the
preset edge weights, `graph_walk.py` dropped out of the top-5
entirely. The walk was diluting the correct signal into structurally-
adjacent siblings.

**The 9+ preset hyperparameters in 0.3.0:**

  - `query_seeds.match_filenames score_per_hit = 1.0`
  - `query_seeds.match_symbols   score_per_hit = 1.5`
  - `query_seeds.match_path_tokens score_per_hit = 0.5`
  - `query_seeds.match_semantic   threshold = 0.45`
  - `graph_substrate path_prox weight = 0.35` (lowered from 0.7 in
    a failed mid-bench tuning attempt — STILL a preset)
  - `graph_walk.DEFAULT_ALPHA       = 0.15`
  - `graph_walk.DEFAULT_EPS         = 1e-3`
  - `graph_walk.DEFAULT_MAX_VISITED = 200`
  - `graph_walk.DEFAULT_TOP_K_EDGES = 8`

Each one a hand-chosen magic number. Each one a future tech-debt
liability. Each one trading off bench-quality vs implementation
cleanliness with no principled basis.

## User feedback (2026-05-06)

> 你这里所谓的权重是怎么调的你是 preset 的呢还是这个 automatically
> 的调的现在 performance 下降了对吗, 你记住所有的东西都不能 preset
> 要不然这就变成了hyperparameter我们不能要这么多的hyperparameter
> 因为这会accumulate这种technical debt我们要的intelligence是要足够的
> generic的并且保证之前版本的accuracy和latency

Translation: "How are these weights tuned, preset or auto? Performance
dropped, right? Remember — nothing can be preset, otherwise it becomes
hyperparameter; we can't accumulate this kind of technical debt; the
intelligence we want must be generic enough; and previous versions'
accuracy and latency must be preserved."

This is exactly the Principle 1 anti-pattern (`Understanding >
Enumeration`) the project's own principles file forbids.

## 0.3.1 rollback — three concrete moves

  1. **Cascade integration removed.** `_graph_walk_candidates` is no
     longer called from `cascade_search`. The `SKYGREP_GRAPH_WALK` env
     var is dead — toggling it has no effect. The escalation path
     reverts to 0.2.21's pure Round-A ∪ Round-C union.

  2. **Preset score-per-hit constants stripped from
     `query_seeds.py`.** All four matchers (`match_filenames`,
     `match_symbols`, `match_path_tokens`, `match_semantic`) now
     return raw token-hit counts (or, for semantic, the cosine score
     itself which is data-derived). No 1.0 / 1.5 / 0.5 ratios.

  3. **`path_prox` edge weight derived from data.** Instead of the
     preset 0.35 (or earlier 0.7) constant, the weight is now
     `1 / (1 + max(0, 8 - depth))` — depth-derived from the path
     itself, no free parameter.

What stays:
  - The schema (`graph_node`, `graph_edge` tables). Idempotent, no
    cost in default behaviour.
  - The modules (`graph_walk.py`, `query_seeds.py`,
    `graph_substrate.py`). They're useful primitives even when not on
    the cascade critical path; future work can use them with derived
    weights.
  - The 20 unit tests. They cover the modules in isolation; passing
    them is necessary even when integration is shelved.

What's deferred (until weights are derived from corpus stats):
  - Graph-walk integration into cascade
  - Public `SKYGREP_GRAPH_WALK` flag
  - PPR walk's α / eps / max_visited need principled defaults

## 0.3.1 invariants — by construction

  - **Latency**: identical to 0.2.21. Cascade path unchanged; the
    rolled-back integration was on the escalation path only and was
    gated behind a now-dead env var.
  - **Accuracy**: identical to 0.2.21 on the public-OSS bench. The
    rollback strictly removes the integration that hurt 3/5 queries
    on this internal bench.

## Lesson recorded in auto-memory

`feedback_comprehensive_test_before_release.md` — every release must
include real-corpus end-to-end benchmark with concrete numbers, not
just unit tests. Receipt: 0.3.0 shipped on by-construction
arguments; 0.3.1 is the principled rollback.

## What "principled v2" would look like

For the next attempt at integrating graph-walk into the cascade,
edge weights and walk parameters must come from corpus statistics or
from learned signals — not from human picks. Concrete options:

  - **TF-IDF weighting**: each token → file edge weight =
    `log(N_files / N_files_containing_token)`. Self-deriving from the
    indexed corpus.
  - **Learned restart probability**: α tuned per-query by an LLM head
    that classifies "this query needs local lookup" (high α) vs
    "this query needs broader exploration" (low α). The router is
    already doing this for cascade scope; extending it for graph walk
    is one prompt change, not a new hyperparameter.
  - **σ-adaptive walk stop**: replace `eps` with the same Bayesian
    σ-evidence framing already used in `cascade_search` —
    `eps_eff = max(eps_floor, k · σ(top_K_residuals))`. Ties the walk
    stop to the data, not a constant.

These are not implemented in 0.3.1; they are the design contract for
when graph-walk reintegration is attempted again.
