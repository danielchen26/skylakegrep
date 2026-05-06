# Plan — Lazy index driven by LLM-inferred entry points

**Date filed:** 2026-05-06
**Status:** open · for 0.5.0
**Supersedes:** all prior graph-walk plans (the post-index overlay
approach was the wrong target; see
`memory/feedback_users_actual_vision.md` for receipts).

---

## 1. The problem 0.5.0 solves (the user's actual vision)

> 即便我们 index 也不用 index 全局
> 我们 adaptively 和 dynamically 地构建 local 的 search space
> 一点一点扩大就像 diffusion process 一样
> 找最可能的 node — 这个 node 可以是 path、folder、各种 file type
> intelligent 地找,intelligent 地扩散

**One-line goal:** `skygrep "<query>"` works on a project that
has **never been indexed**, returning a useful answer in
**seconds** (not minutes), by:

  1. Asking an LLM to pick likely entry-point paths from the
     filesystem tree alone (cheap, no embedding).
  2. Embedding **only** those entry-point files on-demand.
  3. Using σ-evidence to diffuse outward through reference-graph
     neighbours, embedding those lazily too.
  4. Never embedding globally.

## 2. Why 0.2.x → 0.4.x didn't solve this

  - 0.2.x cascade requires `skygrep index .` first → 5-10 min on a
    Django-sized repo before the first query can run.
  - 0.4.0/0.4.1/0.4.2 added 1-hop graph overlay AFTER the full
    upfront index → +2 ms cosmetic improvement, **upfront cost
    unchanged**.

Neither addressed the long-initial-index pain.

## 3. Architecture (one diagram)

```
  skygrep "query"  ───→  in unindexed project
        │
        ▼
   1. Cheap filesystem crawl                  no embedding
        │  list of files, dir tree
        ▼
   2. LLM router (qwen2.5:3b)                 ~200 ms
        │  PROMPT: query + tree summary
        │  RETURN: top-N candidate paths/folders/files
        ▼
   3. Token-shortcut union                    deterministic
        │  filename / path token matches
        ▼  → seed set: 10-50 files
   4. Lazy embed (bge-m3) on seed files       ~5-15 s
        │  cache in chunks + files table
        ▼
   5. Cosine cascade on small embedded pool   <1 s
        │  σ-evidence: confident? exit
        ▼ (if uncertain)
   6. Reference-graph 1-hop diffusion         lazy ref extraction
        │  fetch neighbours' refs from on-disk parse
        │  lazy embed neighbours, add to pool
        ▼ (loop until σ-confident or budget cap)
   7. Final cross-encoder rerank → top-K
```

**No step requires a pre-existing index.** Every step caches what
it produces, so subsequent queries on the same project warm
incrementally.

## 4. What's new vs 0.2.x

| Component | 0.2.x | 0.5.0 |
|-----------|-------|-------|
| `skygrep index` requirement | mandatory before search | optional (eager mode); search works without it (lazy mode) |
| LLM router output | `intent / scope / primary_token` | + `candidate_paths` (new field, list of likely paths/folders) |
| Embedding | upfront, all chunks at once | on-demand per file as the cascade reaches it |
| Reference graph | populated by `populate_graph_table` upfront | populated incrementally as files are embedded |
| First-query latency on fresh repo | 5-10 min (full index) + 1 s | 5-15 s (LLM router + 50 file embeds + cascade) |

## 5. Modules

### NEW: `skylakegrep/src/lazy_indexer.py` (~250 LoC)

```python
def crawl_tree(root: Path) -> TreeSummary:
    """Cheap filesystem walk. Returns dir tree + file list, no I/O on contents."""

def embed_files_on_demand(conn, files: list[Path], embedder, ...) -> int:
    """Embed each file's chunks if not already embedded. Idempotent.
    Caches in the existing chunks + files tables."""

def ensure_refs_for(conn, files: list[Path], root: Path) -> int:
    """Lazy reference-graph: parse refs from these files only, write to graph_edge."""

def lazy_seed_paths(conn, query, tree, router) -> list[Path]:
    """LLM-driven + token-shortcut seed picker (replaces upfront index)."""
```

### EXTEND: `skylakegrep/src/llm_router.py`

  - `RouterDecision.candidate_paths: list[str]` — new field
  - Prompt extension: given `(query, tree_summary)`, output 5-15
    paths most likely to contain the answer.
  - Falls back to filename-token match when LLM unreachable.

### EXTEND: `skylakegrep/src/cli.py` — `search` command

  - When `--lazy` is passed OR no index exists at expected DB
    path: invoke `lazy_indexer.lazy_search(conn, query, root,
    embedder, answerer)` instead of the standard cascade.
  - The lazy_search internally orchestrates steps 1-7.

### DELETE: 0.4.x graph_expand (dead-code cleanup per user)

  - `storage.py:_expand_via_reference_graph()` and its caller in
    `cascade_search` escalation
  - The `graph_edge` population in
    `reference_graph.py:populate_graph_table` (the lazy version
    populates incrementally; the upfront version is dead code)
  - `tests/test_holistic_graph_expand.py`
  - `benchmarks/release-0.4.{0,1}-*.{md,py}` (measured the wrong
    thing)

### KEEP

  - `graph_node` + `graph_edge` schema (used by lazy populator)
  - `populate_symbols`, file-graph PageRank tiebreak — they're
    independent of the lazy/upfront indexing question
  - 0.2.x cascade — runs on the lazy-built pool the same way

## 6. Hyperparameter contract (zero new ones)

Same as the 2026-05-06 holistic plan: every threshold reused, no
new constants:

| Concept | Reuses 0.2.x |
|---------|--------------|
| Similarity score | `cosine(query, file_emb)` |
| When to stop expanding | `σ_topK ≤ CASCADE_TAU_FLOOR` |
| When to escalate to HyDE | unchanged |
| Symbol boost | unchanged |
| Graph tiebreak | unchanged |

The ONLY new judgement is the LLM router's `candidate_paths`
output — and that's an LLM judgement per query, not a global
hyperparameter.

## 7. Bench (the only acceptance metric)

**Test:** time-to-first-answer on a fresh Django checkout where
`skygrep index .` has NEVER been run.

  - Setup: `git clone django; rm -rf ~/.skylakegrep/repos/django-*`
  - Run: `time skygrep search "<query>"` for each of the 10 Django
    benchmark queries (`benchmarks/cross_repo/django.json`)
  - Acceptance:
    - **Latency**: each query < 30 s end-to-end on cold project
    - **Recall**: top-5 hit rate ≥ 7/10 on the Django bench (vs.
      0.2.x published 10/10 with full upfront index — we accept
      ~70 % of recall in exchange for skipping the 5-10 min
      upfront cost)
    - **Embedding count**: < 200 files embedded per query (vs.
      ~5000 for full Django index)

If recall regresses below 7/10, expansion budget gets larger
(more diffusion hops); if still under 7/10, we don't ship.

## 8. Phasing

This plan is **NOT phased** — per the holistic principle, the
whole thing lands in one commit OR not at all. Sub-tasks for
implementation order:

  1. Delete 0.4.x graph_expand dead code
  2. Implement `lazy_indexer.py` core (crawl, embed-on-demand,
     refs-on-demand, lazy_seed_paths)
  3. Extend `RouterDecision` with `candidate_paths` + new prompt
  4. Wire `lazy_search` into `cli.py search` command (auto-mode
     when no index)
  5. Write a single end-to-end test that exercises the lazy
     path on a synthetic fixture (NOT the bench — that's
     real-corpus)
  6. Run the real bench on fresh Django; pass acceptance →
     ship 0.5.0

If any sub-task fails the acceptance, the whole release doesn't
ship.

## 9. Risks

  - **LLM router latency**: 200 ms for the candidate_paths call,
    on top of the existing 50 ms intent classification. Total
    ~250 ms, still cheap relative to embedding cost.
  - **LLM router accuracy**: if the LLM picks the wrong
    directory, we waste embedding budget on irrelevant files.
    Mitigation: token-shortcut filename matches always run too,
    so even an LLM miss has a deterministic fallback.
  - **Cascade with sparse pool**: cosine on 50 files vs. 5000
    files behaves differently (σ distribution is narrower). The
    σ-evidence gate may fire incorrectly. Mitigation: bench it,
    tune `CASCADE_TAU_FLOOR` if needed (existing constant, not a
    new one).

## 10. Decision

Implement in one commit (no phased ship). Bench on fresh Django.
If acceptance passes, ship 0.5.0 with full GH surface update. If
not, document the failure honestly in `benchmarks/` and return to
this plan with concrete failure data.
