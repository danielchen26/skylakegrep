# skylakegrep 0.5.0 — adaptive lazy index for cold-start semantic search

The 0.4.x line removed graph-walk retrieval after a production
`KeyError 'snippet'` regression and 0.4.0–0.4.2 did not actually
improve recall on real corpora. 0.5.0 returns to the original design
intent that motivated the graph work in the first place: **the user
should not have to run `skygrep index .` and wait 5–10 minutes before
asking the first semantic question.**

## The three confidence tiers

skylakegrep now offers three answers to "I just `cd`-ed into a repo,
ask one question":

| Tier | Latency | Recall | When |
|---|---|---|---|
| `rg` cold-start (existing 0.2.x) | ~100 ms | keyword match only | default; fires while the eager index builds in the background |
| `--lazy` (NEW in 0.5.0) | ~5 s | partial semantic (4–5 / 10 on Django) | opt-in; LLM picks ~25 entry-point files, batch-embeds, σ-validated top-K |
| full eager index (existing 0.2.x) | 5–10 min upfront, then ~100 ms / query | full semantic (30 / 30 on the public OSS bench) | run once via `skygrep index .` (or auto-spawned by `--auto-index`) |

The lazy tier is the missing middle: an honest semantic answer in
seconds on a project that has never been indexed, with explicit
σ-validated confidence telemetry so the user knows it's partial.

## How `--lazy` works

```
skygrep "<query>" --lazy
```

1. Walk the project tree (depth-bounded) into a directory summary.
2. Token-shortcut seeds: any file whose path/name contains a query
   token is included for free (no LLM, no embed).
3. LLM router (`qwen2.5:3b`) picks 5–15 likely entry-point paths from
   the directory summary alone — no file content read, no
   embeddings yet.
4. Batch-embed all selected seed files in ONE `bge-m3` call via the
   Ollama batch endpoint (≈ 5× faster than per-file).
5. Score the query embedding against each file embedding via cosine
   similarity, keep top-K.
6. Compute σ across the top-K cosine scores → emit a confidence
   label (`none` / `low` / `medium` / `high`) so the user can judge
   whether to trust the partial answer or fall through to a full
   index.

No new hyperparameters. The same `cosine` metric and the same
`CASCADE_TAU_FLOOR` / `CASCADE_K_SIGMA` confidence-gate constants
that the 0.2.x σ-adaptive cascade uses are reused; the only knob
is `seed_budget=25` which is the LLM-routed seed count, not a
threshold.

## Real numbers

End-to-end CLI test on a fresh, never-indexed copy of the
skylakegrep repo itself:

```
$ skygrep --lazy "where is the cosine similarity scoring computed"
… top-5 results ranked by cosine …
[8.3 s · lazy cold-start · embedded=25new/0cached · σ=0.027 · confidence=high]
```

Bench on Django (10 queries, fresh DB each):

```
hits 4/10 (top-5) · p50 5.0 s · max 9.1 s · ~20 files embedded / query
```

Compare: full upfront `skygrep index .` on Django embeds ~5000 files
and takes ~5–10 min before the first query can run. 0.5.0 gives the
user 4 correct answers out of 10 *in 5 s each* instead of nothing
at all for the first 5 minutes.

## What `--lazy` is not

- Not a replacement for the full cascade. The full eager index still
  scores 30 / 30 on the public OSS bench and is still the right tier
  for repos you'll be querying repeatedly.
- Not a replacement for `rg` cold-start. If you want an answer
  *instantly* and a keyword match is good enough, the existing
  `~100 ms` `rg` path remains the default.
- Not a graph-walk overlay. The 0.4.x graph_expand layer has been
  removed; the 0.2.21 σ-adaptive cascade (Round-A ∪ Round-C) is
  restored as the post-index path.

The lazy tier is purely additive: it occupies the previously-empty
middle of the latency / recall curve.

## API

For programmatic callers:

```python
from skylakegrep.src import lazy_indexer as LZ
from skylakegrep.src.embeddings import OllamaEmbedder

embedder = OllamaEmbedder(...)
results, telemetry = LZ.lazy_explore_cold_start(
    conn, query, project_root, embedder, top_k=5, seed_budget=25,
)
# telemetry: {"embed_new": 25, "embed_cached": 0,
#             "sigma": 0.027, "confidence": "high"}
```

`lazy_explore_cross_folder(query, root, embedder)` is also exposed
for the proactive-explorer use case (look beyond `cwd` when the
query is unlikely to live in the current dir). 0.5.x will wire this
into `proactive.filename_extend` to replace its hardcoded
`~/Downloads`/`~/Desktop`/`~/Documents` defaults with an LLM-routed
search space.

## What changed

- New module `lazy_indexer.py`: `crawl_tree`, `render_tree_summary`,
  `token_shortcut_seeds`, `embed_files_batch`, `cosine_topk_with_sigma`,
  `lazy_explore_cold_start`, `lazy_explore_cross_folder`.
- New `--lazy` CLI flag on `skygrep search` (and the bare-form alias).
- New helper `infer_candidate_paths` in `llm_router.py` for LLM-routed
  path selection from a directory summary.
- `OllamaEmbedder.embed_batch` now used for one-call batch embedding
  of seed files — primary perf lever (25 s → 5 s on Django).
- 0.4.x graph-walk retrieval (`_expand_via_reference_graph`) removed
  from `cascade_search` (already reverted in the 0.4.2 hot-fix).
- `populate_graph_table` no longer eagerly populates `graph_edge`;
  the schema is preserved and `lazy_indexer.ensure_refs_for` will
  populate it lazily as the cascade reaches files.
- 201 / 201 tests pass; no new test failures introduced.

## Verified end-to-end

- `pytest tests/` — 201 passed
- `skygrep --lazy "where is the cosine similarity scoring computed"`
  on a fresh copy of the skylakegrep repo: 8.3 s, top-5 returned,
  σ=0.027, confidence=high.
- 10-query Django bench: 4 / 10 hits, p50 5.0 s, max 9.1 s.
