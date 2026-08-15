# General Performance Benchmark v2

This benchmark answers a narrower, defensible question than “how much faster is
skylakegrep everywhere?”:

> Across pinned public repositories, how much retrieval context and tool work
> does a skygrep-first agent policy use relative to an `rg`-only policy, **after
> both policies meet the same task-quality floor**?

The older 4.05× result remains a six-task deterministic CI receipt. It is not a
general multiplier. General Benchmark v2 separates correctness, context
efficiency, and measured retrieval latency so none can hide a regression in
another dimension.

## Public matrix

The checked-in registry [`../benchmarks/public_repos.json`](../benchmarks/public_repos.json)
pins six public repositories and 60 hand-validated source-evidence tasks:

| Repository | Ecosystem | Tasks |
| --- | --- | ---: |
| Cobra | Go | 10 |
| Django | Python | 10 |
| React | JavaScript / Flow | 10 |
| Spring Framework | Java | 10 |
| Tokio | Rust | 10 |
| Vite | TypeScript | 10 |

Every task has a canonical path, optional accepted alternatives, at least two
literal evidence terms, at least two quality terms, and a public ground-truth
note. `benchmarks/validate_public_fixtures.py` fails if a commit, accepted path,
or evidence term drifts. Reports also record the benchmark source commit and
whether its tracked worktree was clean. Private fixtures remain in the gitignored
`benchmarks/cross_repo/` boundary.

## Conditions

Each task is run in paired conditions against the same repository commit:

1. `skygrep-first`: adaptive structured retrieval, candidate reads, then a
   bounded `rg` fallback only when evidence remains insufficient.
2. `rg-only`: bounded literal-term searches, expansion, then candidate reads.

The default full report uses the real `cl100k_base` tokenizer through
`tiktoken`. `chars/4` remains an explicit compatibility fallback and is always
identified in report metadata. Index build time is recorded separately from
query latency.

## Quality gate before efficiency

A pair enters the efficiency headline only when both policies:

- complete the task;
- achieve task-completion quality of at least 0.85;
- return the required path and literal source evidence.

This is a deterministic retrieval-context quality proxy, not a claim that an
unobserved downstream model wrote a correct final answer. It is deliberately
auditable: paths, literal facts, noise control, sufficiency, and stop behavior
are scored from the captured tool context.

The skygrep aggregate must also be non-inferior to `rg-only` within two
percentage points for both completion and work quality. A general claim
requires at least 30 unique eligible tasks across at least three repositories,
with at least 80% of all paired observations remaining eligible. Repeated
trials improve measurement stability but never inflate the unique-task count.
The benchmark source must also resolve to one full Git commit with a clean
tracked worktree. Parallel receipts are rejected unless all six were produced
from that same source commit.

Only after those gates pass does the report calculate:

- context-token reduction;
- tool-call reduction;
- measured harness wall-time ratio, including deterministic scoring and token
  counting;
- measured retrieval tool-time ratio, captured before token counting;
- cross-task and per-repository P25 / median / P75 after taking each task's
  median across repeated trials;
- a 95% hierarchical-bootstrap interval over repositories and unique tasks,
  after taking each task's median across repeated trials.

The median and ratio-of-sums are both reported. The benchmark never substitutes
`context_tokens / assumed_throughput` for measured latency in its headline.

## Run cadence

This is deliberately not the per-change inner loop:

| Gate | Cadence | Cost |
| --- | --- | --- |
| Deterministic six-task contract | Every pull request | Seconds; model-free |
| General Benchmark v2 | Weekly, before a performance release, or on explicit request | Six full public indexes plus paired trials |

The scheduled General Benchmark runs repositories in parallel. Local reruns
reuse completed indexes unless the index format, embedding model, or pinned
source changes.

## Reproduce

```bash
python -m pip install -e ".[benchmark]"

python benchmarks/validate_public_fixtures.py \
  --oss-root /tmp/skygrep-general-v2-repos \
  --prepare

python benchmarks/universal_closed_loop_benchmark.py \
  --oss-root /tmp/skygrep-general-v2-repos \
  --prepare \
  --refresh-index \
  --reset-index \
  --index-timeout 7200 \
  --trials 3 \
  --tokenizer tiktoken \
  --report /tmp/skygrep-general-v2.json \
  --summary-only

python benchmarks/closed_loop_regression_gate.py \
  /tmp/skygrep-general-v2.json \
  --require-general-reportable
```

The first full run builds six complete semantic indexes and can take hours on a
CPU-only or memory-constrained machine. Index elapsed time is recorded
separately and does not enter the retrieval-efficiency headline. Once the pins
are fully indexed, omit `--refresh-index --reset-index` to repeat query trials
without rebuilding them.

The weekly `General Benchmark v2` GitHub workflow runs the six pinned
repositories as independent parallel jobs with local `bge-m3` embedding
services, then merges their compact rows and recomputes one quality gate and
confidence interval. It uploads both per-repository receipts and the final
merged JSON receipt.

## What this still does not measure

This is a retrieval-workflow benchmark. Its quality gate scores retrieved
source support rather than a model-generated final answer. Its token counts cover tool context,
not hidden model reasoning, final-answer output, cache billing, or provider
pricing. A true provider end-to-end claim additionally requires the same model,
prompt, temperature, agent loop, and provider usage receipts in both
conditions. Such measurements should be reported as a separate benchmark, not
combined with this one.
