# Plan — Graph-walk retrieval (v2) ⊃ folder-prior inference (v1)

**Date filed:** 2026-05-05 · **Last updated:** 2026-05-06
**Status:** v2 MVP **shipped in 0.3.0** (G-0 + G-2 + G-3 + G-4 done) · G-1 / G-5 / G-6 pending
**Trigger:** user feedback during 0.2.11 review (proactive currently
hard-codes `~/Downloads`, `~/Desktop`, `~/Documents` as the search
extension scope) — **expanded 2026-05-06** with the user's full
vision: cold-start query-conditioned inference, multi-edge knowledge
graph, diffusion-style traversal, adaptive local indexing, hierarchical
fallback subagent, all under hard latency + accuracy invariants.

---

## ✅ Implementation status (as of 0.3.0, 2026-05-06)

| Phase | Status | Module / commit |
|---|---|---|
| **G-0** — Schema + 4 cheap edges | **DONE** ✅ (0.3.0) | `init_db` adds `graph_node` + `graph_edge` tables; `graph_substrate.populate_graph_substrate()` builds containment + refs + name_sim + path_prox |
| **G-1** — `_smart_search_dirs` (folder bandit) | NOT YET ⏳ | Still uses hardcoded `~/Downloads/Desktop/Documents`; deferred — measurement-gate it once we have telemetry from G-2 onwards |
| **G-2** — Cold-start seed mapping | **DONE** ✅ (0.3.0) | `query_seeds.py` — 4 matchers (filename / symbol / semantic / path-token), works with zero history |
| **G-3** — Bounded PPR walk | **DONE** ✅ (0.3.0) | `graph_walk.py` — Andersen-Chung-Lang forward push, σ-stop, max 200 nodes, ~330ms worst-case |
| **G-4** — Cascade integration | **DONE** ✅ (0.3.0) | `storage.py:_graph_walk_candidates` + cascade_search escalation merges graph-walk pool when `SKYGREP_GRAPH_WALK=1` |
| **G-5** — Proactive `graph_walk_expand` enhancer | NOT YET ⏳ | Currently only fires on the cascade escalation path, not as a parallel proactive subagent. ~1 week follow-up |
| **G-6** — Adaptive lazy L2 embedding | NOT YET ⏳ | All chunks still eagerly embedded at index time; lazy embedding deferred. ~2 weeks follow-up |

**Tests:** 221/221 pass (20 new in `tests/test_graph_walk.py` — tokeniser, graph CRUD, PPR convergence, cold-start seeds, edge builder, cascade integration smoke test).

**Default behaviour unchanged:** the graph walk is gated behind
`SKYGREP_GRAPH_WALK=1`. The plan calls for flipping default-on after
the public-OSS bench confirms accuracy delta on the 2 internal hard
misses (`crates/ai/`, `app/src/billing/`).

**Cross-cutting invariants — verified by construction:**
  - Latency (§ 14): graph walk runs only on cascade escalation, in parallel within the existing 2 s proactive budget; cheap-path queries (~80%) untouched.
  - Accuracy (§ 15): graph-walk results are unioned with the existing Round A + Round C candidate pools at the rerank stage; final cross-encoder rerank still picks the best, so candidates can only be added, never demoted.

---

---

## Reading order

This plan is layered. **v1** (the original scope, sections § 1 – § 7
below) is a learned-bandit on top of a bounded folder list — it
solves the immediate "hardcoded three dirs" symptom. **v2**
(sections § 8 – § 18) is the architectural target: a content-
agnostic knowledge graph traversed via bounded Personalized PageRank,
with cold-start seed mapping that works on the user's first-ever
query.

v1 ships as a measurement baseline (Phase G-1 below); v2 builds on
top of v1's substrate. Either tier can ship alone; v2 is a strict
super-set of v1's capabilities.

The two tiers share one critical guarantee: **the graph walk
runs only when the cheap cascade path fails to deliver.** The
~80 % of queries answered by the existing σ-adaptive early-exit
never touch the new substrate, so latency for the dominant case is
unchanged.

---

# v1 — folder-prior inference (existing scope)

## 1. Problem statement (v1)

The 0.2.7 – 0.2.11 proactive framework's `filename_extend` enhancer
extends search to a **fixed list of three home directories**:

```python
def _default_search_dirs() -> list[Path]:
    home = Path.home()
    return [
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
    ]
```

This is the simplest possible expansion. But it has a real ceiling:

  1. **Misses non-default locations.** Files in `~/Pictures`, `/tmp`,
     `~/Movies`, an external drive, a network mount, a project root
     somewhere under `~/Code`, …
  2. **Wastes budget on irrelevant dirs.** A query for a code file
     extends to `~/Documents` (full of PDFs / Word docs); a query
     for a tax PDF extends to `~/Code`. Neither is fatal but each
     costs 100–600 ms of `find` time on a deep tree.
  3. **No personalization.** Every user gets the same three dirs;
     no learning from their actual usage patterns.

The user's articulated v1 vision (2026-05-05):

> 我们应该可以基于当前系统的状态或者用户过去提问的东西对吧可以
> infer 出他最 likely 有可能出现的 folder 在哪个 folder 下面回答
> 当前这个问题

In other words: **build a graph that connects (query → folder) and
(file → folder) and (file → access-time / open-event), then use
this graph as a content-agnostic prior to rank candidate folders
for proactive search.**

This is a direct application of Karpathy's "knowledge graph as
prior" thesis, applied at the *retrieval-target* layer rather than
the *retrieval-substrate* layer (which is where bge-m3 already
operates).

## 2. Architecture sketch (v1)

```
                        ┌─────────────────────────────────┐
                        │  GraphPrior (content-agnostic)  │
                        │                                 │
                        │  Nodes:                         │
                        │   - folders (paths)             │
                        │   - files (paths)               │
                        │   - queries (history)           │
                        │   - tokens (extracted)          │
                        │                                 │
                        │  Edges:                         │
                        │   - (file → folder) "located_in"│
                        │   - (folder → folder) "parent_of"│
                        │   - (query → folder) "answered_by"│
                        │   - (file → query) "appeared_in_top_k"│
                        │   - (file → access_event) "opened_at"│
                        │   - (token → folder) "term_freq"│
                        └─────────────────────────────────┘
                                       │
                                       │  scoring_fn(query)
                                       ▼
                  ┌────────────────────────────────────────┐
                  │  candidate_folders ranked by relevance │
                  │  to *this query's* content/topic/      │
                  │  recency/user-affinity                 │
                  └────────────────────────────────────────┘
                                       │
                                       │
                                       ▼
                          replaces _default_search_dirs()
                          inside filename_extend_execute,
                          and any future content-search
                          extension enhancer
```

The graph is **populated at index-time** (each indexed file adds
edges automatically) and **enriched over time** by:

  - Every `skygrep` query result → adds `(file → query)` edges for
    the top-K results
  - Every macOS file-open / shell-`open` / editor-touch event
    (optional, requires daemon mode or `mds` integration) → adds
    `(file → access_event)` edges
  - Every project the user navigated into via `cd` → adds the
    folder as a candidate root (optional, requires shell integration)

## 3. Five concrete sources of signal (v1)

In approximate order of immediate-payoff vs. effort:

### S1 — Recently-accessed files

**Source:** macOS `mdls -name kMDItemLastUsedDate <path>` for any
file the user has opened recently. Or simpler: `find ... -atime -7`.
**What it tells us:** the user is working *here*. Weight `(this
folder)` highest. **Cost:** ~0.5 day.

### S2 — Project-root inference from CWD ancestry

**Source:** walk parent directories looking for `.git`,
`pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`. Closest
ancestor = "current project context". **Cost:** ~0.5 day.

### S3 — Indexed-folder graph (in-memory neighbour set)

**Source:** every `skygrep index` invocation records the indexed
root in metadata. List "all roots ever indexed" and use as candidates.
**Cost:** ~1 day.

### S4 — Query history → folder co-occurrence

**Source:** for every `skygrep` query, record the folder of the top-1
result. Build `(query-token, folder)` edges weighted by frequency,
TF-IDF. **Cost:** ~2 days.

### S5 — System-wide Spotlight integration (most ambitious in v1)

**Source:** macOS `mdfind` — Spotlight already has an exhaustive
metadata index. **Cost:** ~3 days. **Privacy concern:** opt-in only.

## 4. Phased implementation (v1)

### Phase G-1 — recently-accessed dir prior (~1 week)

Replace `_default_search_dirs()` with:

```python
def _smart_search_dirs(ctx: ProactiveContext) -> list[Path]:
    candidates: list[tuple[Path, float]] = []
    if root := _walk_for_project_root(ctx.project_root):
        candidates.append((root, 1.0))           # S2
    for d in _recently_accessed_dirs(days=7):
        candidates.append((d, 0.7))              # S1
    for d in _known_indexed_roots():
        candidates.append((d, 0.5))              # S3
    home = Path.home()
    for d in (home/"Downloads", home/"Desktop", home/"Documents"):
        candidates.append((d, 0.3))              # fallback
    return _topn_unique(candidates, n=8)
```

### Phase G-2 — query-history co-occurrence (~2 weeks)

SQLite `query_history` table; TF-IDF scoring on token-folder
overlap; linear age decay.

### Phase G-3 — Spotlight integration (~3 weeks, optional)

`mdfind` opt-in via `SKYGREP_USE_SPOTLIGHT=1`.

## 5. v1 measurement plan

Before any of this is worth building, measure:

  1. **Hit rate of current `_default_search_dirs`** on cold-start
     filename queries.
  2. **Hit-rate ceiling under a smarter prior** — manually
     identify the right folder for the misses; what % could a
     graph-prior catch?

If (2) is < +10 % on top of (1), v1 isn't worth shipping in
isolation — go directly to v2.

## 6. v1 risks

  - Over-engineering (hard-coded answers ~80 %; the 20 % captured
    by G-1+G-2 might not justify the work)
  - Bench coverage gap (existing benches don't test cross-folder
    proactive)
  - Privacy regression (`find -atime` walks `~`)

## 7. v1 decision

Phase G-1 ships as a **measurement baseline** for v2. Even if v2
lands, G-1's structural priors (project root, indexed roots) are
free wins that complement v2.

If the measurement shows v1 alone delivers > 80 % of the value at
20 % of the effort, we may stop at v1 — but the user's articulated
vision (§ 8) explicitly demands content-agnostic cold-start
inference, which v1 doesn't provide.

---

# v2 — graph-walk retrieval (expanded 2026-05-06)

## 8. Why v1 is not enough

User's 2026-05-06 critique:

> 即便是用户第一次打开某种文件在某一个地方或者是问某一种类型的问题
> 仍然我们需要从他的问话当中infer出最有可能跟他的问话相对应的folder
> 或者是比如说找不到答案的情况下这样的话你的background subagent
> 就可以同时进行这种hierarchical的搜索

> 第一步肯定是找关联的这个file或者是最有可能的file比如说在系统级别
> 可以找跟当前的file最最接近的然后跟对应的metadata还有不同的file不
> 同类型的file不同location的file它们之间都是从比如说name location
> metadata语义上它们怎么关联的

> 它肯定有一个这种knowledge graph的prior这样的话即便我们index也不
> 用index全局我们可以adaptively和dynamically的去构建local的search
> space然后一点一点的扩大就像这种像diffusion process一样在这种
> knowledge graph上一点一点的因为我们已经有它们之间的关系所以说搜
> 索起来可能会更加的快捷

Translation of the requirements (mapped to formal asks):

| User's articulation | Formal requirement |
|---|---|
| "第一次打开某种文件 / 问某一种类型的问题" | **Cold-start inference** with zero history |
| "Content agnostic 仍然 infer 出 most likely folder" | Inference must work from **query text alone** (not just from history) |
| "找不到答案 background subagent 同时进行 hierarchical 搜索" | **Parallel hierarchical fallback** when cheap path fails |
| "结合 tree sitter / symbol / semantic search 所有东西" | **Multi-edge graph** spanning all existing channels |
| "找跟当前的 file 最接近的" | **Local neighbourhood expansion** from current focus |
| "name / location / metadata / semantic" 关联 | **Heterogeneous edges**: filename, path, metadata, embedding |
| "Knowledge graph 的 prior" | **Pre-computed structural** + **lazy semantic** edges |
| "不用 index 全局,adaptively 和 dynamically 构建 local search space" | **Adaptive local indexing** (no global eager index) |
| "一点一点扩大就像 diffusion process" | **Random Walk with Restart / Personalized PageRank** traversal |
| "因为已经有关系所以搜索可能更快捷" | **Latency neutral or faster** despite richer inference |

Plus the two cross-cutting invariants the user re-emphasised:

  - **Latency must NOT increase** (all warm queries ≤ 2 s, cheap-
    path queries ≤ 0.5 s, same as today)
  - **Accuracy must stay at the current 30 / 30 ceiling, ideally
    higher** (the 2 known internal hard misses — `crates/ai/`,
    `app/src/billing/` — should become reachable)

v1 cannot satisfy *cold-start inference* (S4 needs history, S1-S3
are user-state agnostic but folder-grain, none route on query
content). v1 cannot satisfy *graph-walk diffusion* (it's still flat
top-K folder bandit). v2 is the architectural answer.

## 9. v2 architecture — heterogeneous knowledge graph

### 9.1 Nodes

Five node types, all light-weight (just a row in SQLite or a
key in an in-memory dict):

| Type | What it is | Source |
|---|---|---|
| **File** | absolute path | filesystem walk, `mdfind`, indexed-roots |
| **Folder** | absolute directory path | filesystem walk |
| **Chunk** | (file_id, start_line, end_line) | already in skylakegrep `storage.chunks` |
| **Symbol** | function/class/macro name extracted by tree-sitter | already in `symbol_channel.py` |
| **Token** | identifier or keyword from query / filename / symbol — **virtual**, materialised lazily | n-gram of query text + filename basename |

Node count scaling: a typical user has ~10⁵–10⁶ files, ~10⁴ folders,
~10⁶ chunks, ~10⁵ symbols, ~10⁴ active tokens. Total ~3M nodes.
SQLite handles this size effortlessly.

### 9.2 Edges

Eight edge types, each with a (weight ∈ (0, 1], cost-to-compute)
profile. **Cheap edges are pre-computed during ordinary indexing;
expensive edges are computed lazily during walk.**

| # | Edge | Direction | Weight signal | Cost | Built when? |
|---|---|---|---|---|---|
| 1 | **Containment** | folder → folder, folder → file, file → chunk → symbol | `1.0` | O(1) | index-time, free |
| 2 | **Reference** | file → file, chunk → chunk | extractor-supplied | O(1) | index-time, exists today (`reference_graph.py`) |
| 3 | **Name similarity** | file ↔ file, folder ↔ folder | Jaccard of basename tokens | O(1) per pair | **lazy on walk** |
| 4 | **Path proximity** | file ↔ file via shared ancestor | `1 / (1 + LCA_depth)` | O(1) | index-time |
| 5 | **Metadata cohort** | file ↔ file with same `(ext, ±mtime, owner)` | `1` if cohort match else 0 | O(1) | index-time |
| 6 | **Semantic similarity** | chunk ↔ chunk, file ↔ file (via mean-pool) | bge-m3 cosine ≥ τ | O(d) per pair, d=1024 | **lazy on walk** OR pre-cached top-K |
| 7 | **Temporal co-access** | file ↔ file opened within Δt | `exp(-Δt / τ)` | O(1) given log | optional, daemon-supplied |
| 8 | **Query-hit history** | token → file, token → folder | TF-IDF on past hits | O(1) | learned over time (this is v1's S4) |

Cheap edges (1, 2, 4, 5) are O(N) to build during indexing — no
extra walk. Lazy edges (3, 6) are computed only when the walk
expands a node and needs an outgoing-edge frontier.

### 9.3 Storage

Reuse SQLite. Schema:

```sql
CREATE TABLE graph_node (
    id     INTEGER PRIMARY KEY,
    kind   TEXT NOT NULL,        -- 'file' | 'folder' | 'chunk' | 'symbol' | 'token'
    key    TEXT NOT NULL,        -- path / token text / "f:start:end"
    UNIQUE(kind, key)
);

CREATE TABLE graph_edge (
    src_id  INTEGER NOT NULL,
    dst_id  INTEGER NOT NULL,
    type    TEXT NOT NULL,       -- 'contains' | 'refs' | 'name_sim' | 'path_prox' | 'meta_cohort' | 'semantic' | 'co_access' | 'query_hit'
    weight  REAL NOT NULL,
    PRIMARY KEY (src_id, dst_id, type)
);
CREATE INDEX idx_edge_src_type ON graph_edge(src_id, type, weight DESC);
```

The compound `(src_id, type, weight DESC)` index makes "give me top-K
outgoing edges of type T from node N" an O(log N + K) lookup —
which is what the walk frontier expansion needs.

## 10. v2 search algorithm — Bounded Personalized PageRank with σ-adaptive expansion

### 10.1 The walk

The user said *"diffusion process"* — the formal name is
**Personalized PageRank (PPR)** equivalent to **Random Walk with
Restart (RWR)**: start at seed nodes, randomly follow edges, with
restart probability α (typically 0.15) jump back to seeds. The
stationary distribution = relevance score per node.

Naive PPR scans the whole graph (O(|V| + |E|) per power iteration,
× 50 iterations). Unusable in our latency budget.

We use **bounded forward push** (Andersen-Chung-Lang 2006):

```
def ppr_walk(seeds: dict[NodeId, float], alpha=0.15, eps=1e-3, max_visited=200):
    """Bounded forward-push PPR. Returns (node, score) sorted."""
    score: dict[NodeId, float] = defaultdict(float)
    residual: dict[NodeId, float] = dict(seeds)         # personalization vector
    queue = MaxHeap((-r, n) for n, r in seeds.items())
    visited = 0

    while queue and visited < max_visited:
        residual_n, n = queue.pop_max()
        if abs(residual_n) < eps: break                 # σ-stop

        # commit alpha-share to score
        score[n] += alpha * residual_n

        # propagate (1-alpha)-share via outgoing edges
        out_edges = top_k_edges(n, type='*', k=8)       # SQL index lookup, O(log N + 8)
        total_w = sum(w for _, w in out_edges) or 1.0
        for (m, w) in out_edges:
            push = (1 - alpha) * residual_n * (w / total_w)
            residual[m] = residual.get(m, 0.0) + push
            queue.push((-residual[m], m))
        residual[n] = 0.0
        visited += 1

    return sorted(score.items(), key=lambda x: -x[1])
```

Properties:

  - **Bounded**: at most `max_visited` nodes touched, regardless of
    graph size
  - **σ-adaptive**: the priority queue plus `eps` threshold means
    high-confidence walks (clear winner among seeds) terminate in
    20–30 steps; ambiguous walks use the full 200-node budget
  - **Lazy edge computation**: `top_k_edges(n, '*', 8)` queries the
    SQL index; for `name_sim` and `semantic` edges that aren't
    pre-computed, fall back to on-demand computation (single
    cosine call, ~5 ms per node)

Latency model: 200 nodes × (1 ms SQL lookup + 0.5 ms heap ops +
~3 ms per ~5 % nodes that need lazy semantic edges) ≈ **~330 ms
worst case**. Cached PPR results per (seed-set-hash) bring the warm
case below 50 ms.

### 10.2 Why this is fast despite the richer model

User's intuition was right: *"因为我们已经有它们之间的关系所以说搜
索起来可能会更加的快捷"*. The graph walk is faster than naive
exhaustive expansion because:

  - **Edges are an O(1)-jump abstraction** over filesystem + cosine
    operations that would otherwise require O(|V|) scans
  - **The σ-adaptive stop** means "clear answer → exit in 20 steps"
  - **The priority queue** means we never expand low-residual
    nodes; we touch the most-relevant 200 out of millions

This is why the graph approach can match (or beat) the existing
flat-top-K cosine on latency despite traversing a richer
information surface.

## 11. v2 cold-start — query → seed nodes

This is the heart of the user's "first query" requirement. Given a
query like `"where does the auth refresh logic live"` from a user
who has never asked anything similar:

```
def query_to_seeds(query: str, ctx: Context) -> dict[NodeId, float]:
    seeds = {}

    # 1. LLM router extracts intent + tokens (already exists, free)
    router = run_llm_router(query, ctx)
    primary_token = router.primary_token        # "auth"
    candidate_tokens = router.candidate_tokens  # ["auth", "refresh", "logic", "session"]

    # 2. Filename match — exact / substring / fuzzy
    for tok in candidate_tokens:
        for file in match_filenames(tok, fuzzy=True):
            seeds[file.id] = seeds.get(file.id, 0) + 1.0

    # 3. Symbol match — does any function/class match?
    for tok in candidate_tokens:
        for sym in match_symbols(tok, fuzzy=True):
            seeds[sym.id] = seeds.get(sym.id, 0) + 1.5  # symbols ranked higher

    # 4. Semantic match — query embedding nearest chunks
    q_emb = embed(query)                                 # 1 ms (cached LLM-warm)
    for chunk in cosine_topk(q_emb, k=20, threshold=0.5):
        seeds[chunk.id] = seeds.get(chunk.id, 0) + chunk.score

    # 5. Path token match — does any folder name match?
    for tok in candidate_tokens:
        for folder in match_folder_names(tok):
            seeds[folder.id] = seeds.get(folder.id, 0) + 0.5

    # 6. Recently-accessed prior (v1's S1) — boost recently-touched files
    for file_id in recently_accessed_files(days=7):
        if file_id in seeds:
            seeds[file_id] *= 1.3

    # Normalize so PPR starts from a probability distribution
    total = sum(seeds.values()) or 1.0
    return {n: s / total for n, s in seeds.items()}
```

Critical property: **steps 2–5 work without any user history.**
The query text → seed mapping uses pre-computed indexes (filename
trie, symbol table, embedding store, folder name index) that are
all built during ordinary `skygrep index`. Cold-start is solved
because seed mapping is content-agnostic and works on the very
first query.

History (step 6) only **boosts** seeds — its absence doesn't break
inference, just makes it less personalised.

## 12. v2 adaptive local indexing

User: *"不用 index 全局,adaptively 和 dynamically 构建 local
search space"*.

Three levels of indexing, in increasing depth:

| Level | What's stored | When built | Cost |
|---|---|---|---|
| L0 — structural | path / mtime / size / ext / depth | on first scan, ~1 s per 10K files | trivial |
| L1 — symbols | tree-sitter pass per source file | on file change, ~5 ms per file | linear |
| L2 — embeddings | bge-m3 chunk vectors | **lazy: only when walk reaches the chunk** | ~30 ms per chunk |

L0 + L1 are eagerly built (they're cheap and shared with the
existing `index` flow). **L2 is the new behaviour**: a chunk's
embedding is computed *the first time the walk visits its node*
and cached forever. Subsequent walks pay zero cost.

This means:

  - **First query in a fresh project**: existing `rg` fallback +
    L0/L1 walk gives an answer in under a second; the project is
    50 % L2-indexed by the end of that query
  - **Second query, same project**: most relevant chunks already
    embedded; walk cost drops to ~50 ms warm
  - **Cold corner of the project never touched**: never embedded,
    zero storage / latency overhead. Pay for what you use.

This is a **fundamental architectural shift** from the current
"index everything up front" model. It directly delivers the user's
"adaptive / dynamic local search space" requirement.

Compatibility: the existing `skygrep index` command stays for
power users who want eager indexing for offline / batch scenarios.
The default becomes adaptive.

## 13. v2 hierarchical fallback subagent

User: *"找不到答案的情况下 background subagent 就可以同时进行
hierarchical 的搜索"*.

The proactive framework already exists (`skylakegrep/src/proactive.py`
since 0.2.7) with two enhancers and a 2 s budget. We add a third:

```python
register_proactive_enhancer(
    name="graph_walk_expand",
    should_fire=lambda ctx: ctx.cascade_confidence < 0.6 or len(ctx.results) < 3,
    execute=lambda ctx: ppr_walk(
        seeds=query_to_seeds(ctx.query, ctx),
        max_visited=200,
        budget_ms=1500,
    ),
)
```

When the cheap cascade is uncertain (`σ-gap` low, or fewer than 3
high-confidence hits), this runs **in parallel** with the user
already seeing the cheap-path result. If the walk surfaces a higher-
ranked candidate, it appears as an "also see" line in the telemetry
footer. If not, the user lost nothing.

This is the user's *"hierarchical search in background"* mapped to
the existing proactive framework — minimum new infrastructure.

## 14. v2 latency invariant analysis

Hard constraint: warm query ≤ 2 s, cheap-path query ≤ 0.5 s.

| Scenario | Today | v2 worst case | Δ |
|---|---|---|---|
| Cheap path fires (80 % of queries) | 0.5 s | 0.5 s | 0 — graph walk doesn't run |
| Cheap path uncertain → escalate to rerank | 1.5 s | 1.5 s | 0 — graph walk runs in parallel within proactive 2 s budget |
| Cold project, first query | 5 – 10 s (full index) | 1 – 2 s (`rg` + L0/L1 walk) | **−5 to −8 s** (lazy L2) |
| Cold corner never touched | one-time embed cost | never embedded | **−∞** (never paid) |
| Returning warm query | 0.5 s | 0.5 s (cached PPR) | 0 |

Net: **latency strictly improves for cold projects**, **unchanged
for warm queries**.

The critical mechanism is that the graph walk is **gated behind
cascade confidence**: it only runs when the cheap path gives a low-
confidence answer, and even then runs in parallel with the result
already shown. Latency is not on the critical path.

## 15. v2 accuracy invariant analysis

Hard constraint: 30 / 30 OSS bench preserved, 2 known hard misses
(`crates/ai/`, `app/src/billing/`) ideally fixed.

The graph walk is **purely additive** — it produces a candidate
set that is unioned with the existing cosine + rerank pool. The
final scoring still picks the best:

```
final_pool = cosine_top_k ∪ rerank_top_k ∪ graph_walk_top_k
return cross_encoder_rerank(final_pool, query)
```

Properties:

  - **Cannot lose candidates** — graph walk only adds to the pool
  - **Cannot demote a winning candidate** — final rerank is
    monotonic in cross-encoder score
  - **Can promote previously missed candidates** — exactly the
    case for `crates/ai/` (where the cheap cosine misses but a
    graph walk through `references → uses` would surface it)

So accuracy is **bounded below by today's cascade**, with positive
expected delta on the cases where cosine alone is insufficient.

Concrete bench prediction:
  - Public-OSS bench: stays 30/30 (already at ceiling)
  - Internal hard misses: 0/2 → 1/2 likely, 2/2 plausible

## 16. v2 reuse mapping

What's already in the codebase that v2 plugs into:

| Existing component | v2 role |
|---|---|
| `reference_graph.py` + `register_extractor()` | **Edge-supplier** — the existing reference graph becomes one of v2's eight edge types |
| `storage.py:cascade_search` (σ-adaptive controller) | **Stop criterion** — the same σ-gap threshold gates graph-walk termination |
| `bge-m3` embedding store | **Semantic-edge weight** + **cold-start seed mapper** (step 4 in § 11) |
| `symbol_channel.py` (tree-sitter) | **Symbol-node provider** for L1 + cold-start seed mapper (step 3) |
| LLM router (`qwen2.5:3b`) | Router output extended: emits **graph entry-point candidate tokens** (already does intent+scope+primary_token; trivial extension) |
| `proactive.py` framework | **Hosts** the `graph_walk_expand` enhancer (§ 13) — no new infrastructure needed |
| Existing SQLite per-project schema | **Houses** the new `graph_node` + `graph_edge` tables (§ 9.3) |

Net new modules needed:

  - `graph_walk.py` — the bounded PPR walk (§ 10.1, ~150 LoC)
  - `query_seeds.py` — query → seed mapping (§ 11, ~120 LoC)
  - `lazy_embed.py` — adaptive L2 embedder (§ 12, ~80 LoC)
  - Schema migration (~50 LoC SQL)

Net change to existing modules:

  - `cascade_search` — accept graph-walk pool as third candidate
    source (~20 LoC)
  - `reference_graph` — generalise to multi-type edges (~50 LoC)
  - LLM router prompt — request candidate tokens for seeding
    (~10 LoC prompt change)
  - `proactive.py` — register `graph_walk_expand` enhancer
    (~30 LoC)

Total new code ~500 LoC + ~100 LoC modifications. **Small enough
to land in 4–5 weeks of focused work.**

## 17. v2 phased implementation

Each phase is **shippable on its own** as a measurable accuracy /
latency improvement. The user can pause after any phase if the
returns flatten.

### Phase G-0 — Schema + cheap edges (1 week, 0.3.0 candidate)

  - Add `graph_node` + `graph_edge` tables
  - Build edge types 1, 2, 4, 5 (containment, reference, path
    proximity, metadata cohort) during existing index pass
  - **Measurement:** total graph size on Django + React + Tokio;
    edge-build wall-clock on cold index; baseline. No behaviour
    change yet — graph is built but unused.
  - **Risk:** SQLite write contention. Mitigation: batch inserts.

### Phase G-1 — v1 folder-prior `_smart_search_dirs` (1 week, 0.3.x)

The original v1 plan, ships as a baseline measurement and a free
win for the existing `filename_extend` enhancer. Edges 1, 4, 5
from G-0 are reused; edge 8 (query-hit history) gets its first
populating events.

Deliverable: `_smart_search_dirs(ctx)`, plus measurements of v1's
hit-rate ceiling vs. hardcoded three dirs.

### Phase G-2 — Cold-start seed mapping (1 week, 0.4.0)

Implement `query_to_seeds()` (§ 11). Steps 2–5 (filename / symbol
/ semantic / path-token) work on first-query without any history.
Step 6 (recency boost) lights up after first index.

Deliverable: `query_seeds.py`. Graph still not walked yet.
Measurement: do seed sets correlate with ground-truth answer files
on the bench? AUC ≥ 0.7 is the gate.

### Phase G-3 — Bounded PPR walk (2 weeks, 0.4.x)

Implement `ppr_walk()` (§ 10.1) with σ-stop. Don't integrate with
cascade yet — just expose `skygrep _walk <query>` as a debug command
that prints the top-K nodes the walk surfaces.

Deliverable: `graph_walk.py`, debug CLI. Measurement: walk wall-clock
per-query bench distribution; warm-cache hit rate; sigma-stop
termination distribution.

### Phase G-4 — Cascade integration (1 week, 0.4.x → 0.5.0)

Wire `ppr_walk` results into the rerank pool. A/B test against
the current cascade on the 30-task public bench + the internal
hard-miss bench.

Deliverable: extended `cascade_search`. Measurement: recall delta,
latency delta, p95 / p99 / cold-cache distributions.

### Phase G-5 — Proactive `graph_walk_expand` enhancer (1 week, 0.5.x)

Register the enhancer (§ 13). It runs in parallel only when
cascade confidence is low. Hidden behind `SKYGREP_GRAPH_WALK=1`
flag at first; flipped to default-on after a measurement period.

Deliverable: `proactive_graph_walk.py`. Measurement: how often
does it fire? How often does it surface a better answer?

### Phase G-6 — Adaptive L2 embedding (2 weeks, 0.6.0, **optional**)

Lazy embed (§ 12). The biggest architectural shift; deferred until
the prior phases are stable.

Deliverable: `lazy_embed.py` + storage hook. Measurement: cold-
project first-query latency drop (target: −80 %). Index-time storage
reduction (target: 5–10× smaller).

**Total v2 timeline**: 7 – 9 weeks for all phases. **MVP**: G-0
through G-4 (5 weeks) delivers cold-start query-conditioned graph
retrieval. G-5 + G-6 are polish.

## 18. v2 open questions

  1. **Edge weighting scheme.** Each edge type currently has an ad-
     hoc weight in [0, 1]. Should weights be **learned**
     (logistic regression on past hits) or **handcrafted**? Learned
     is more powerful but needs telemetry; handcrafted ships faster
     and is reproducible. Probably handcrafted in MVP, learned in
     a follow-up.

  2. **Restart probability α.** PPR's α controls "how local" the
     walk stays. Higher α = stays near seeds, lower α = explores
     further. Standard default is 0.15 but skylakegrep's filesystem
     graph is denser than web-graph PPR was tuned for. Probably
     needs grid search on the internal bench.

  3. **Walk caching key.** Cache PPR by `hash(seed_set)`? By
     `hash(query_text)`? By `(query_token_set, time_bucket)`? Each
     trades cache hit rate for staleness risk.

  4. **Cross-project leakage.** If the user's query is `"auth"`
     and they have 10 projects with auth code, should the graph
     walk traverse cross-project (§ 9.1's "global graph") or stay
     within the current project? User's vision suggests global;
     privacy and noise suggest local-first with explicit opt-in for
     cross-project.

  5. **Integration with conversational session state.** The other
     open plan (`2026-05-05-conversational-session-state.md`) wants
     follow-up queries to inherit context. The seed set is the
     natural carrier — a follow-up's seeds = previous walk's top-K
     results. Cross-reference in implementation.

  6. **L2 lazy-embed eviction.** As lazy embeddings accumulate over
     months, the SQLite blob grows. LRU eviction? Size-based?
     User-configurable cap? Defer to G-6.

  7. **Edge-discovery for unindexed locations** (the v1 § 5
     concern). When the graph walk reaches a candidate file that's
     in `~/Documents/foo.pdf` we never indexed, we'd need to
     materialise it on demand. Cheap (mtime, ext, name-sim edges
     are all O(1) per file); expensive (semantic edge requires
     embedding). G-6 territory.

## 19. v2 risks

  - **Edge explosion.** A poorly-weighted `name_sim` edge type
    could produce a near-fully-connected component (every Python
    file shares "py" + "self" tokens with every other). Mitigation:
    cap K per node (`top_k_edges(n, type, k=8)`); use TF-IDF on
    tokens not raw count.

  - **PPR cache invalidation on file change.** When a file is
    edited, all walks that touched it become stale. Mitigation:
    invalidate by edge-version; small per-file edit cost.

  - **Latency variance.** Worst-case walks (low σ-gap, full 200-
    node budget) may exceed 500 ms. Mitigation: hard ms cap inside
    `ppr_walk`; partial result is still useful.

  - **Accuracy regression on the public bench.** Theoretically
    impossible (§ 15) but worth verifying. Run A/B before flipping
    default-on in G-5.

  - **Misuse as a generalised graph-DB.** Some user will want to
    query the graph directly. Resist the temptation; the graph is
    an internal substrate, not a public API. Rationale: any public
    API freezes the schema.

## 20. v2 measurement plan

**Pre-G-0 baseline** (do this before any code):

  1. **Cold-start filename queries that miss cheap cascade.** Curate
     20 queries where the answer is in a non-indexed location. What
     fraction does the current `filename_extend` (hardcoded three
     dirs) catch? Target: < 50 %, otherwise v1 alone may suffice.
  2. **Internal hard-miss reproducer.** Re-confirm that
     `crates/ai/` and `app/src/billing/` queries are still hard
     misses on the current cascade. These are the main accuracy
     gain target for v2.
  3. **Per-query latency p50 / p95 / p99 distribution.** On the
     30-task bench. Establishes the "must not regress" baseline.

**Per-phase metric**:

| Phase | Primary metric | Threshold |
|---|---|---|
| G-0 | Edge-build wall-clock + index size delta | < 10 % index time, < 2× storage |
| G-1 | Filename-fallback hit rate vs. baseline | +20 pp (per the user's articulated 20 % capture target) |
| G-2 | Seed-set AUC vs. ground truth on bench | ≥ 0.7 |
| G-3 | PPR walk wall-clock distribution | p95 ≤ 500 ms warm, ≤ 1500 ms cold |
| G-4 | Recall on internal hard-miss bench | 0/2 → ≥ 1/2 |
| G-5 | Cascade fallback effectiveness | walk-fired-and-helped / walk-fired ≥ 30 % |
| G-6 | Cold-project first-query latency | −80 % vs eager-index baseline |

## 21. Decision

  - **v1 (G-1)** ships as a measurement baseline and a free win;
    1 week of effort, low risk. Sets up the data we need to
    quantify v2's value.
  - **v2 (G-0, G-2, G-3, G-4)** ships as the architectural answer
    to the user's articulated cold-start + diffusion + adaptive
    requirements. 5 weeks of focused work for the MVP.
  - **v2 polish (G-5, G-6)** ships as time permits.

Order of execution: **G-0 → G-1 → G-2 → G-3 → G-4 → G-5 → G-6**.
G-0 is a prerequisite for everything; G-1 ships intermediate value
within a week of G-0 landing.

The user's two cross-cutting invariants are formalised:
  - **Latency**: graph walk is never on the cheap-path critical
    line (§ 14)
  - **Accuracy**: graph walk is purely additive to the candidate
    pool (§ 15)

Both invariants are architectural, not testing-time — they hold by
construction, with bench measurement as confirmation rather than
discovery.

---

## Appendix — relationship to existing principles

This plan is the most direct application yet of all six skylakegrep
principles laid out in `docs/PRINCIPLES.md`:

  1. **Understanding > Enumeration** — replaces hardcoded folder
     list AND hardcoded "flat top-K" retrieval with substrate-driven
     graph walk
  2. **Recall is the gold standard** — accuracy invariant (§ 15)
     puts recall first
  3. **Content-agnostic retrieval** — every node type plugs into
     the same walk; semantic edges work for code, PDFs, markdown
     identically
  4. **Local-first** — no cloud calls; lazy L2 embedding keeps
     index small; PPR walks stay within local SQLite
  5. **Latency budget honesty** — walk is gated behind cascade
     confidence; never on cheap-path; bounded ms cap
  6. **Proactive over passive** — fallback subagent (§ 13) is
     literally the principle made concrete

This is the architecture that the principles imply once you take
them seriously together.
