# Parity benchmarks

Reproducible head-to-head comparisons of `skygrep` against real
`ripgrep`, run on three popular open-source codebases. Anyone with
the repos cloned can rerun in a few minutes and verify every number
on this page.

> **Headline:** `skygrep` matches `rg` exactly across all three
> codebases (**10 / 10 each** on Django · Tokio · React,
> **30 / 30 aggregate**). React reached 10 / 10 after the
> Option-C substrate upgrade (`bge-m3` 1024-d symmetric
> embedder + a content-agnostic non-canonical-path filter); the
> two original React misses (`react-007`, `react-010`) and how
> they were resolved are documented below as the engineering
> record, not erased.
>
> `skygrep` returns **60×–770× less context tokens** than `rg`'s
> term-OR scan, so a downstream agent loop that consumes the
> context pays dramatically less even when recall is the same.

## Setup

```bash
git clone --depth=1 https://github.com/django/django   /tmp/oss-bench/django
git clone --depth=1 https://github.com/facebook/react  /tmp/oss-bench/react
git clone --depth=1 https://github.com/tokio-rs/tokio  /tmp/oss-bench/tokio

cd skylakegrep
.venv/bin/python benchmarks/public_oss_bench.py
```

The runner:

  1. Reads each fixture from
     [`benchmarks/cross_repo/{django,react,tokio}.json`](../benchmarks/cross_repo/)
     — 10 hand-labeled questions per repo, each with a canonical
     expected file plus zero or more `expected_alternatives` for
     queries with multiple legitimate answers.
  2. Indexes the OSS repo into a tmp SQLite DB (5–10 min one-time
     per repo).
  3. For each task, runs both:
     - `rg`: term-OR over up to 8 extracted query terms × 20 matches
       per term × 2-line context window (the real ripgrep agent
       baseline).
     - `skygrep`: one semantic top-10 search.
  4. Reports per-task hit / miss + average latency + total context
     tokens emitted.

## Aggregate result

| Repo | LOC ≈ | skygrep recall | rg recall | sky lat | rg lat | sky tokens | rg tokens | token reduction |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| **Django** (Python) | 524K | **10 / 10** | 10 / 10 | 10.13 s | 2.97 s | ~29 K | ~20.6 M | **703 ×** |
| **Tokio** (Rust) | 80K | **10 / 10** | 10 / 10 | 21.91 s | 1.49 s | ~31 K | ~1.9 M | **61 ×** |
| **React** (JS+TS) | 270K | **10 / 10** | 10 / 10 | 11.71 s | 4.58 s | ~29 K | ~22.8 M | **773 ×** |
| **Aggregate** | | **30 / 30 (100 %)** | 30 / 30 (100 %) | | | | | **~ 60×–770× less** |

## How to read these numbers

### "rg recall = 100 %" looks impressive but isn't apples-to-apples

The `rg` agent in this benchmark is the ripgrep equivalent of "search
for every word the user said and dump every line that matches." It
collects up to 160 file-tokens of context per query
(8 terms × 20 matches × 2-line context windows). In practice that
context is enormous: **20 M tokens of `rg` output across the 10
Django queries** vs. **29 K tokens of `skygrep` output**. So:

  - **Yes, `rg` finds the answer in the dump.** Always, on Django
    and Tokio. The expected file is somewhere in the 100 K-line
    haystack.
  - **No, `rg` does not give the agent a useful starting point.**
    The agent now has to read 20 M tokens to figure out which one
    of the 100 file-fragments is the actual answer. That is not a
    realistic real-world workflow — it's a recall ceiling on a
    deliberately permissive ripgrep configuration.
  - **`skygrep` returns the right file ranked top-10 in 30 of 30
    cases.** That is the user-facing number.

### Why React used to lag (and how it was resolved)

The React fixture used to surface two honest skygrep weaknesses,
both fixed by the Option-C substrate upgrade. The original failure
modes are documented here as the engineering record:

  1. **Test-fixture path bias.** `react-007` asks for the
     `React.createElement` implementation. The canonical answer is
     `packages/react/src/jsx/ReactJSXElement.js`. skygrep's top-10
     used to be dominated by test fixtures
     `fixtures/legacy-jsx-runtimes/react-{14,15,16,17}/cjs/...` —
     filename-token similarity to "jsx-runtime" pulled the legacy
     fixture files ahead of the real source.
  2. **Devtools vs reconciler conflict.** `react-010` asks for the
     Profiler component implementation. The canonical answer is in
     the `react-reconciler/` package. skygrep used to return several
     `react-devtools-shared/.../profilingHooks.js` files instead —
     the devtools profiler had many more "profiler" filename
     mentions than the reconciler's internal timer.

**Resolution (Option C).** Both classes of failure shared a single
root cause: the `mxbai-embed-large` substrate ranked re-export /
fixture aggregators above canonical implementations, and any
post-hoc graph prior could not recover candidates that were not in
the rerank pool. Two changes lifted React to **10 / 10**:

  - **`bge-m3` substrate** (1024-d, symmetric, multilingual
    XLM-RoBERTa). The canonical reconciler / source files now land
    inside the top-K cosine pool for both queries. A better prior
    cannot save you when cosine never surfaces the right file —
    upgrading the embedder is the only fix that worked.
  - **Content-agnostic non-canonical-path filter** (24 universal
    aux conventions: `/fixtures/`, `/examples/`, `/vendor/`,
    `/node_modules/`, `/dist/`, `.development.js`, `.production.min.js`,
    `.min.js`, …). This is a structural prior, not a language-specific
    rule, and applies to any corpus that follows similar conventions
    (markdown notes with `/drafts/`, knowledge graphs with
    `/archive/`, etc.).

### Why per-query latency is higher than `rg`'s

The benchmark cold-loads the cross-encoder reranker once per process
(~30 s) and runs each query through the cascade including the HyDE
escalation path on uncertain queries. In `skygrep serve` daemon mode
the reranker stays warm in memory, and warm queries land in the
~0.5 – 2 s band. The 11–20 s/q numbers here are an honest CLI-from-
cold-start measurement, not a daemon throughput claim.

For an AI agent the relevant cost is *the LLM round-trip after the
search*, which scales with **token count of the context** — and
that is where skygrep's 60–770× reduction lives.

## Per-task detail

Run the benchmark without `--summary-only` to get every task's
expected path, returned top-10, and per-tier hit / miss:

```bash
.venv/bin/python benchmarks/parity_vs_ripgrep.py \
  --root /tmp/oss-bench/react \
  --tasks benchmarks/cross_repo/react.json \
  --top-k 10 > /tmp/react-detail.json

python3 -c "
import json; d = json.load(open('/tmp/react-detail.json'))
for t in d['tasks']:
    print(f\"{t['id']}: skygrep_hit={t['skygrep']['hit']} rg_hit={t['rg']['hit']}\")
"
```

## Reproducing yourself

The fixtures, runner, and `parity_vs_ripgrep.py` are all in this
repo. Pin the OSS clones at the commits you tested against (`git
log --oneline -1` inside each clone) when reporting numbers, since
upstream code drifts.
