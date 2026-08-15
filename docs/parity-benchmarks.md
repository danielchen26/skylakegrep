# Parity benchmarks — historical receipt

This page preserves the earlier three-repository benchmark as an engineering
record. Its 30/30 recall and 60×–770× context figures used moving repository
tips, `chars / 4` token estimates, and a deliberately broad term-OR `rg` dump.
They are **not the current general-performance claim** and should not be
expected to reproduce byte-for-byte on today's sources.

The current release-scale protocol is
[General Benchmark v2](general-performance.md): six exact public commits, 60
source-evidence tasks, real tokenizer counts, paired quality gates, measured
latency, and confidence intervals.

> **Historical result:** `skygrep` matched `rg` across all three
> codebases (**10 / 10 each** on Django · Tokio · React,
> **30 / 30 aggregate**). React reached 10 / 10 after the
> Option-C substrate upgrade (`bge-m3` 1024-d symmetric
> embedder + a content-agnostic non-canonical-path filter); the
> two original React misses (`react-007`, `react-010`) and how
> they were resolved are documented below as the engineering
> record, not erased.
>
> In that historical run, `skygrep` returned **60×–770× less estimated
> context** than `rg`'s term-OR scan. This is the archived result, not a
> current general multiplier.

## Current pinned reproduction

```bash
cd skylakegrep
.venv/bin/python benchmarks/public_oss_bench.py \
  --prepare --tokenizer tiktoken \
  --oss-root /tmp/skygrep-general-v2-repos
```

The runner:

  1. Reads each fixture from
     [`benchmarks/public_tasks/`](../benchmarks/public_tasks/)
     — 10 source-evidence questions per pinned repo, each with a canonical
     expected file plus zero or more `expected_alternatives` for
     queries with multiple legitimate answers.
  2. Validates the public origin, exact commit, clean tracked tree, accepted
     paths, and literal evidence before running either policy.
  3. For each task, runs both:
     - `rg`: term-OR over up to 8 extracted query terms × 20 matches
       per term × 2-line context window (the real ripgrep agent
       baseline).
     - `skygrep`: one semantic top-10 search.
  4. Reports per-task hit / miss + average latency + total context
     tokens emitted.

## Historical aggregate result

| Repo | LOC ≈ | skygrep recall | rg recall | sky lat | rg lat | sky tokens | rg tokens | token reduction |
| --- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| **Django** (Python) | 524K | **10 / 10** | 10 / 10 | 10.13 s | 2.97 s | ~29 K | ~20.6 M | **703 ×** |
| **Tokio** (Rust) | 80K | **10 / 10** | 10 / 10 | 21.91 s | 1.49 s | ~31 K | ~1.9 M | **61 ×** |
| **React** (JS+TS) | 270K | **10 / 10** | 10 / 10 | 11.71 s | 4.58 s | ~29 K | ~22.8 M | **773 ×** |
| **Aggregate** | | **30 / 30 (100 %)** | 30 / 30 (100 %) | | | | | **~ 60×–770× less** |

## Worked example — `django-001` (one query, real numbers)

This worked example preserves the original measured values from one of the 30
legacy tasks. The old page did not pin the source commit, so the values are an
engineering receipt rather than a promise of byte-identical reproduction.
Run the current pinned protocol above to produce a new identified receipt.

> **Query**: "Where does Django turn an incoming URL into the view
> function that should handle it?"
> **Expected canonical**: `django/urls/resolvers.py`
> (`URLResolver.resolve()`)
> **Vocab mismatch**: query says "URL into view", code identifier is
> `resolve` — the failure mode that grep-as-search collapses on.

### Side A — `rg` term-OR scan (the rg-agent baseline)

The rg-agent extracts up to 8 terms (TF-IDF-ish stopword filter),
runs `rg -i -F --max-count=20 -C2` per term, concatenates the
output. For this query the extractor produced:

```
['function', 'incoming', 'incom', 'django', 'handle', 'should', 'into', 'that']
```

The high-signal words `URL`, `view`, `resolve` did not survive the
extractor — they were either stopword-filtered or pushed past the
8-term cap. That's the vocab-mismatch failure: the query's actual
intent is `URLResolver.resolve()`, but `rg` searches for `function`,
`django`, `that` instead.

Real measured output volumes per `rg` invocation (one per term):

| `rg` term     | Output chars | Tokens (chars / 4) |
| --- | --: | --: |
| `function`    |     1,811,753 |       452,938 |
| `incoming`    |        16,095 |         4,024 |
| `incom`       |       254,373 |        63,593 |
| `django`      |     5,752,979 |     1,438,245 |
| `handle`      |       952,033 |       238,008 |
| `should`      |     1,694,468 |       423,617 |
| `into`        |       700,156 |       175,039 |
| `that`        |     3,364,369 |       841,092 |
| **Total**     | **14,546,226** | **3,636,556** |

`django` alone is **1.4 million tokens** because the term matches in
basically every file of the Django source tree. `that` is **840 K
tokens** for the same reason — high-frequency words that the
stopword filter let through.

### Side B — `skygrep --top 10 --json`

```bash
skygrep "Where does Django turn an incoming URL into the view function that should handle it?" \
  --json --top 10
```

Real measured output: **10,430 chars ≈ 2,607 tokens**. Top files
returned include `django/urls/resolvers.py` and related canonical
implementation files.

### Reduction

| | Tokens | Notes |
| --- | --: | --- |
| `rg` term-OR scan | **3,636,556** | dominated by `django` (1.4 M) and `that` (840 K) — stopwords flooded the haystack |
| `skygrep --top 10` | **2,607** | top-K cosine over `bge-m3` substrate, no token-level matching |
| **Ratio** | **≈ 1,395 ×** | for this query |

This is **higher** than the 60 × – 770 × headline range because
vocab-mismatch queries are the worst case for `rg` (stopwords flood
the output) and the best case for `skygrep` (the embedder bridges
"URL into view" → `resolve()`).

### Why the headline range is 60 × – 770 ×, not a single number

`rg` output volume scales with **(repo LOC) × (term-frequency of the
high-signal terms in the query)**. `skygrep` output is
~constant-per-K (top-10 ≈ 10 KB of JSON):

| Repo | LOC | Per-query rg tokens (avg) | Per-query skygrep tokens | Ratio |
| --- | --: | --: | --: | --: |
| **Tokio** (Rust) | 80 K | ~190 K | ~3.1 K | **61 ×** |
| **Django** (Python) | 524 K | ~2.06 M | ~2.9 K | **703 ×** |
| **React** (JS+TS) | 270 K | ~2.28 M | ~2.9 K | **773 ×** |

Tokio is the floor (small repo, focused vocabulary). Django and
React both blow up because `django` / `react` saturate term-OR scans
across mid-sized monorepos.

### Inspect the legacy calculation (not an exact reproduction)

```bash
# rg side — counts bytes from term-OR scan
cd /tmp/oss-bench/django
for term in function incoming incom django handle should into that; do
  rg -i -F --max-count 20 -C 2 "$term" .
done | wc -c

# skygrep side — counts bytes from top-10 JSON
skygrep "Where does Django turn an incoming URL into the view function that should handle it?" \
  --json --top 10 | wc -c

# divide chars by 4 to approximate tokens
```

The original source commit was not pinned, so a current clone is not expected
to reproduce the table within a fixed tolerance. These commands only expose
the historical `chars / 4` calculation; use the pinned runner above to create
a new receipt.

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
escalation path on uncertain queries. `skygrep serve` now binds before
the optional heavyweight reranker is imported. Pass `--warm-reranker`
to load it once in the background for a rerank-heavy daemon workload;
agent-fast/context queries do not need it. The 11–20 s/q numbers here
remain an honest CLI-from-cold-start measurement, not a daemon
throughput claim.

For an AI agent, context volume can affect the downstream LLM round-trip. The
60–770× range above is the result recorded by this historical approximation;
current claims must use the pinned General Benchmark v2 protocol.

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
