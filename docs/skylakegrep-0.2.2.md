# skylakegrep 0.2.2 — release notes

`0.2.2` makes embedder upgrades non-blocking and surfaces every
search's routing decision back to the user. Two changes, both
content-agnostic, both UX wins.

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact <chentianchi@gmail.com>.

## What changed

### 1. Intelligent recovery — no more `--reset`-shaped UX cliff

Before `0.2.2`, upgrading the default embedder (e.g. `nomic-embed-text`
768-d → `bge-m3` 1024-d in the `0.1.x` → `0.2.x` jump) left the index
in a half-stale state and required the user to know they had to run
`skygrep index <repo> --reset`. That command then **blocked the
shell for 10–30 minutes** on a mid-sized repo while it re-walked the
filesystem, re-chunked, re-embedded, re-extracted symbols, and
re-built the file-export graph — even though only the per-chunk
vector actually had to change.

`0.2.2` replaces that with an automatic background worker:

  - **Detection is automatic.** Every `skygrep search` compares the
    embedder fingerprint (`model_name:dim`) stored in the index's
    new `metadata` table against the active embedder's fingerprint.
    Mismatch triggers recovery; matched is a no-op.

  - **The user is never blocked.** The first query after detection
    returns instantly via the existing rg-fallback / cascade-cheap
    path. The recovery worker runs in a background daemon thread
    and re-embeds chunks in place. Already-recovered files become
    semantically searchable as soon as their batch commits — the
    `_filter_to_matching_dim` helper added in `0.2.1` filters
    stale-dim chunks out of every search, so coverage grows
    monotonically while the user keeps working.

  - **Priority order is mtime-DESC.** Recently-modified files
    re-embed first. User behaviour is Pareto-distributed: the next
    ~80 % of queries hit the most-recently-touched ~5 % of files,
    so semantic coverage of the queries the user actually runs
    reaches near-100 % long before the long tail finishes. On a
    35K-chunk medium repo this means the first 30 seconds of
    background work covers what the user is realistically going to
    search next.

  - **Crash-safe and resumable.** Worker progress lives in the
    `metadata` table (`recovery_progress`, `recovery_eta_seconds`,
    `recovery_coverage_pct`, `recovery_heartbeat_at`). The next CLI
    invocation picks up where a crashed worker died: the worker's
    SQL is `length(vectors.embedding) / 4 != expected_dim` rather
    than tracking its own offset, so files re-embedded by an
    earlier run naturally drop out of the queue.

  - **Re-embedding skips the embedder-independent work.** Tree-sitter
    chunking, symbol extraction, and the file-export graph don't
    care about the vector space — only the per-chunk embedding has
    to change. Net wall-time is ~30–60 % less than `--reset`.

The implementation is the new module `skylakegrep.src.recovery`
(detection + worker + footer renderer) plus three new helpers in
`skylakegrep.src.storage` (`get_meta`, `set_meta`,
`count_stale_chunks`, `count_total_chunks`) and a new `metadata`
table in `init_db`.

`skygrep index <repo> --reset` is still supported and still works.
You just don't have to think about it any more.

### 2. Routing transparency — every result tells you which path it took

The CLI's per-query telemetry footer now leads with the actual
retrieval path (the headline routing decision the user actually
cares about), the σ-evidence reason behind the cascade's choice,
the recovery state if a worker is active, and a `quality=BEST` /
`quality=DEGRADED-recovery` tag.

Before:

```
[0.51s · router=llm · intent=mixed (0.83) · 1 filename + 0 lexical
       + cascade · cascade=cheap (gap=0.020 τ=0.015)
       · index 20s ago · 36 files · L2 symbols on · graph prior on]
```

After (normal query):

```
[0.42s · path=cosine-cheap · router=llm · intent=mixed (0.83)
       · 1 filename + 0 lexical
       · σ-gap=0.0820 ≥ τ=0.0050 (adaptive) → high-confidence early-exit
       · index 20s ago · 36 files · L2 symbols on · graph prior on
       · quality=BEST]
```

After (during recovery):

```
[0.18s · path=cosine-cheap · router=rule · intent=lexical (0.41)
       · 0 filename + 1 lexical
       · σ-gap=0.0030 < τ=0.0050 (adaptive) → escalated to rerank
       · index 20s ago · 36 files · L2 symbols on
       · recovery=in-progress chunks=8127/35131 coverage=23% ETA=14m12s
       · quality=DEGRADED-recovery]
```

The new fields:

  - **`path=`** — the retrieval strategy that answered this specific
    query. Currently one of `cosine-cheap`, `cosine-escalated-rerank`,
    `rg-only`, or `cascade-skipped`. The symbol-channel path will
    appear in `0.3.x` once the auto-router lands.

  - **`σ-gap=… → reason`** — the Bayesian evidence that drove the
    cascade's choice. High σ-gap = top-K candidates are well-separated
    → cascade trusts cosine and exits cheaply; low σ-gap = candidates
    are tied → cascade escalates to the cross-encoder reranker.

  - **`recovery=in-progress chunks=N/T coverage=N% ETA=Nm`** — only
    appears while a recovery worker is active. Reads live from the
    metadata table on every render so the numbers reflect the
    moment the result printed, not the moment the search started.

  - **`quality=BEST` / `quality=DEGRADED-recovery`** — at-a-glance
    indicator of whether the result is the full-quality semantic
    answer or a partial one taken during recovery. `DEGRADED` only
    means some files are still pending re-embed; the user's query
    might be unaffected if it landed on already-recovered files
    (which it usually does, thanks to the mtime-DESC priority).

## Compatibility

- **Python**: ≥ 3.9 (unchanged)
- **Ollama**: still defaults to `bge-m3`. The recovery worker is
  embedder-agnostic — it reads `embedder.model_name` /
  `embedder.model` and the dim returned by the first probe embed.
- **Existing 0.2.0 / 0.2.1 indexes**: a no-op upgrade. The `metadata`
  table is added by `init_db`; on first `0.2.2` query the embedder
  fingerprint is recorded.
- **Indexes from before 0.2.0** (mxbai-embed-large 1024-d, or
  nomic-embed-text 768-d): on first `0.2.2` query, the recovery
  worker auto-spawns and brings the index forward to `bge-m3`
  in the background. **No `--reset` required any more.**

## Reproduce / verify

The bench numbers are unchanged from `0.2.1` (no retrieval-quality
changes in this release):

| Repo | Recall | Latency |
| --- | :-: | :-: |
| Django | 10 / 10 | 10.13 s/q |
| Tokio | 10 / 10 | 21.91 s/q |
| React | 10 / 10 | 11.71 s/q |
| **Aggregate** | **30 / 30 (100 %)** | **~14.6 s/q** |

```bash
git clone --depth=1 https://github.com/django/django   /tmp/oss-bench/django
git clone --depth=1 https://github.com/facebook/react  /tmp/oss-bench/react
git clone --depth=1 https://github.com/tokio-rs/tokio  /tmp/oss-bench/tokio

ollama pull bge-m3
.venv/bin/python benchmarks/public_oss_bench.py
```

## Known follow-ups (not in 0.2.2)

- **Phase C** (intelligent retrieval — symbol channel auto-router OR
  smarter cascade scheduler OR content-agnostic structural-channel
  registry): tracked in
  [`docs/plans/2026-05-05-phase-c-audit.md`](plans/2026-05-05-phase-c-audit.md)
  with three candidate paths (A / B / C) and a ranking. User-pick
  before work begins; slated for `0.3.x`.
- Re-render `docs/assets/{benchmark,schema,hero-dark,og-image}.svg`
  to reflect the `bge-m3` defaults — visual assets, separate
  cosmetic pass.
- Re-run the self-test bench (`benchmarks/agent_context_benchmark.py`)
  on `bge-m3` and update `docs/token-benchmarking.md` top-k 5 row.
- Fix the GitHub Actions `PYPI_API_TOKEN` secret (currently 403);
  manual `twine upload` works fine in the meantime.
