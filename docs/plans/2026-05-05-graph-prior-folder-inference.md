# Plan — Graph-prior folder inference for proactive enhancement

**Date filed:** 2026-05-05
**Status:** Open · design complete, no code
**Trigger:** User feedback during 0.2.11 review (proactive currently
hard-codes `~/Downloads`, `~/Desktop`, `~/Documents` as the search
extension scope; user wants a smarter layer that *infers* the most
likely folder for the current query)

---

## Problem statement

The 0.2.7–0.2.11 proactive framework's `filename_extend` enhancer
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

The user's articulated vision:

> 我们应该可以基于当前系统的状态或者用户过去提问的东西对吧可以
> infer 出他最 likely 有可能出现的 folder 在哪个 folder 下面回答
> 当前这个问题

> ("We should be able to use the current system state or the user's
> past queries to *infer* the folder most likely to answer the
> current question.")

In other words: **build a graph that connects (query → folder) and
(file → folder) and (file → access-time / open-event), then use
this graph as a content-agnostic prior to rank candidate folders
for proactive search.**

This is a direct application of Karpathy's "knowledge graph as
prior" thesis, applied at the *retrieval-target* layer rather than
the *retrieval-substrate* layer (which is where bge-m3 already
operates).

---

## Architecture sketch

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

---

## Five concrete sources of signal

In approximate order of immediate-payoff vs. effort:

### S1 — Recently-accessed files (cheapest signal)

**Source:** macOS `mdls -name kMDItemLastUsedDate <path>` for any
file the user has opened recently. Or simpler: `find ... -atime
-7` (files accessed in the last 7 days).

**What it tells us:** The user is working in *this folder*. Any
proactive search should weight `(this folder)` highest.

**Cost to implement:** ~0.5 day. `find -atime` is already a single
subprocess; we already do `find -iname`.

**Failure mode:** macOS doesn't update `atime` reliably without
`noatime` mounts. Better to use `mds` / `mdls`.

### S2 — Project-root inference from CWD ancestry

**Source:** Walk parent directories of the user's `cwd` looking for
markers (`.git`, `pyproject.toml`, `package.json`, `Cargo.toml`,
`go.mod`). The closest such ancestor is "the user's current
project context".

**What it tells us:** If the user is in `~/Code/foo/src/cli/`, they
probably want files from `~/Code/foo/`, not `~/Downloads`.

**Cost to implement:** ~0.5 day. We already do this for the
in-project `project_root`; just expose it to proactive.

### S3 — Indexed-folder graph (in-memory neighbour set)

**Source:** Every `skygrep index` invocation records the indexed
root in the metadata table. Build a list of "all roots this user
has ever indexed" and use them as the candidate set for proactive
extension.

**What it tells us:** If the user previously indexed
`~/Code/<repo-C>` and `~/Code/local-mgrep`, those are
high-affinity candidates for any query.

**Cost to implement:** ~1 day. Need a lightweight per-user
`~/.skylakegrep/known_roots.json` (or extend the SQLite metadata
table).

### S4 — Query history → folder co-occurrence

**Source:** For every `skygrep` query, record the folder that
held the result the user *clicked* (or, lacking click telemetry,
the folder of the top-1 hit). Build `(query-token, folder)` edges
weighted by frequency.

**What it tells us:** A query like `"auth refresh"` historically
got answered from `~/Code/<repo-C>/`; weight that folder
higher for the next `"auth"` query.

**Cost to implement:** ~2 days. Need persistent query history,
TF-IDF-style scoring, decay of old observations.

### S5 — System-wide spotlight integration (most ambitious)

**Source:** macOS `mdfind` or `mds_stores` — Spotlight already has
an exhaustive metadata index of every file on the system. For any
query token, we can ask Spotlight for "files anywhere with this
content / filename token".

**What it tells us:** Everything. Spotlight's index is the
ground-truth filesystem-and-content database the user already has.

**Cost to implement:** ~3 days. Need to spawn `mdfind`, parse its
output, dedup against our own index, and gate behind a privacy
flag (since `mdfind` will scan everything including private dirs).

**Privacy concern:** The user has to opt in. Spotlight scope can
be configured by the user system-wide; we should respect that.

---

## Phased implementation

### Phase G-1 — recently-accessed dir prior (0.3.x?, ~1 week)

Replace `_default_search_dirs()` with a smarter version:

```python
def _default_search_dirs(ctx: ProactiveContext) -> list[Path]:
    home = Path.home()
    candidates: list[tuple[Path, float]] = []
    
    # 1. Current cwd ancestor with project marker (S2) — score 1.0
    project_root = _walk_for_project_root(ctx.project_root)
    if project_root:
        candidates.append((project_root, 1.0))
    
    # 2. Recently-accessed dirs (S1) via `find ~ -atime -7 -type d`
    for d in _recently_accessed_dirs(home, days=7):
        candidates.append((d, 0.7))
    
    # 3. Known indexed roots (S3) — score 0.5
    for d in _known_indexed_roots():
        candidates.append((d, 0.5))
    
    # 4. Fallback: classic ~/Downloads + ~/Desktop + ~/Documents — score 0.3
    for d in (home/"Downloads", home/"Desktop", home/"Documents"):
        candidates.append((d, 0.3))
    
    # Dedup, sort by score, return top-N (5? 8?)
    return _topn_unique(candidates, n=8)
```

Deliverable:
  - `proactive._smart_search_dirs(ctx)` function
  - `find -atime -7 -type d` helper, cached for ~5 min
  - Unit tests against synthetic atimes
  - End-to-end test: synthetic project at deeper-than-default dir

### Phase G-2 — query-history co-occurrence (0.4.x, ~2 weeks)

Add a per-project SQLite table:

```sql
CREATE TABLE query_history (
    query_norm  TEXT,
    folder      TEXT,
    score       REAL,
    last_seen   REAL
);
```

Update on every search: for top-1 result, record `(normalized
query token set, parent folder, +1 score, now)`. Decay all rows
linearly with age.

When proactive extends, score candidate folders by token overlap
with this history.

Deliverable:
  - New table + migration
  - Read/write helpers in `storage.py`
  - Updated `_smart_search_dirs(ctx, query)` to query history
  - 0.4.x release with privacy-mode flag

### Phase G-3 — Spotlight integration (0.5.x or never, ~3 weeks)

Spawn `mdfind` for the query token; merge with our results;
respect Spotlight scope.

Privacy-gate via `SKYGREP_USE_SPOTLIGHT=1`. Default off — opt-in
only.

Deliverable: maybe. May not be worth it; G-1 + G-2 might cover
80 % of value at 30 % of cost.

---

## Open questions

  1. **Where does the graph live?** In the per-project SQLite, in
     `~/.skylakegrep/global_graph.db`, or both? Per-project means
     graph is lost when project is deleted; global means
     cross-project leaks.

  2. **Does the user trust us with home-wide atime data?** Some
     users would rather not have skygrep walking `find -atime` over
     `~`. Privacy gate via env var, default opt-in or opt-out?

  3. **How does this interact with Phase C (intelligent retrieval)?**
     Phase C is about *what to do with the candidates we already
     have*; this plan is about *which folders to consider as
     candidates*. They're orthogonal but compose.

  4. **Should this be content-agnostic per Principle 1, or
     should it use the LLM router to score folders for a query?**
     The content-agnostic answer (TF-IDF on token-folder) is
     cheaper and reproducible; the LLM-based answer is smarter but
     more expensive. Do both, with LLM as primary and TF-IDF as
     offline fallback (the same shape as 0.2.6's out-of-scope
     classification).

  5. **Latency budget.** Proactive's 2000 ms total is already a
     lot. G-1's `find -atime` adds another ~500 ms on a busy home
     dir. We need to either parallelise it with the existing
     `find -iname`, or accept a higher budget on the cold-start
     content-query case.

---

## Risks

  - **Over-engineering.** Hard-coded three dirs answer 80 % of
    real queries. The 20 % we'd capture with G-1+G-2 might not be
    worth ~3 weeks of effort. Need a measurement to justify.

  - **Bench coverage.** None of the existing benchmarks
    (Django/React/Tokio public OSS, self-test) test cross-folder
    proactive. We'd be optimising a metric we don't measure. Need
    to add a "find this file when it's 3 dirs away from the cwd"
    bench.

  - **Privacy regression.** Walking `~` with `find -atime` reads
    file metadata for paths the user might not want skygrep to
    know about. Privacy gate must be explicit and visible (e.g.
    `skygrep doctor` lists the dirs being walked).

  - **`mdfind` scope leak.** If we ever do G-3, Spotlight will
    happily return files from anywhere — including private ones.
    Must respect `mdfind -onlyin` to bound it.

---

## Measurement plan (preqrequisite to Phase G-1)

Before any of this is worth building, measure:

  1. **Hit rate of current `_default_search_dirs`**. Across a
     sample of real cold-start filename queries, what % return
     non-empty hits in `~/Downloads / ~/Desktop / ~/Documents`?
  2. **Hit-rate ceiling under a smarter prior.** Manually
     identify the right folder for the misses. What % could a
     graph-prior have caught?

If (2) is < +10 % on top of (1), the work isn't worth it. If it's
> +30 %, the work is clearly justified.

---

## Decision

This plan is **not scheduled for any specific release**. It is an
open architecture document filed in `docs/plans/` per the user's
instruction:

> 所有的我们的plan应该都写详细的写到这个folder下面都要记录下来

The user said "记录下来" — record this — so it's written down
for the next time we revisit proactive enhancements (probably
after 0.3.x lands its current scope or after a measurement run
proves the value).

The current 0.2.11 ships the prerequisite infrastructure
(`ProactiveContext` with `recovery_state`, the `ctx`-aware
runner). When Phase G-1 lands, it'll fit naturally into that
contract by adding new fields to `ProactiveContext` (`atime_dirs`,
`project_root_inferred`, etc.) without breaking existing
enhancers.
