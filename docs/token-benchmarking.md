# Benchmark protocol

This document defines what the included benchmarks measure, how they are run,
and what they explicitly do not measure.

> **See also:** [`docs/parity-benchmarks.md`](parity-benchmarks.md) consolidates
> the headline numbers, including the real-ripgrep comparison and the
> cross-repo (warp) results, in a single table.

The benchmarks live in `benchmarks/` in the project root:

- `benchmarks/token_savings.py` — retrieval-layer context compression.
- `benchmarks/agent_context_benchmark.py` — agent-style context-gathering
  comparison against a *simulated* grep-agent baseline.
- `benchmarks/parity_vs_ripgrep.py` — same comparison against a *real*
  `rg` baseline; supports cross-repo task lists via `--tasks`.
- `benchmarks/parity_vs_mixedbread.py` — Mixedbread cloud parity harness;
  requires interactive `mgrep login` and is not run by CI.

## Headline result

![local-mgrep benchmark](assets/benchmark.svg)

At top-k 10 on the deterministic context-gathering benchmark in this
repository:

```text
mgrep hit rate:                       30/30
grep hit rate:                        30/30
estimated total-token reduction:      2.00×
context-token reduction:              2.90×
mgrep tool calls:                      30
grep-agent tool calls:                 227
```

The result is reproducible from a fresh clone. It is not an end-to-end
provider billing measurement; see [Limitations](#limitations) below.

## What is measured

There are two distinct measurements, intentionally kept separate.

### 1. Retrieval-layer context compression

`benchmarks/token_savings.py` reports two ratios:

```text
indexed_context_reduction_x      = indexed corpus tokens / retrieved JSON tokens
source_doc_context_reduction_x   = source + docs tokens / retrieved JSON tokens
```

The numerator approximates the size of context that an agent would otherwise
read; the denominator approximates the size of the top-k JSON snippets
returned by `mgrep search`. Token volumes are estimated as `chars / 4`.

This measures how much smaller the *retrieved* context is than the *whole
indexed corpus*. It does not measure full agent session usage; it does not
count planning prompts, tool-call wrappers, repeated searches, follow-up
file reads, final answers, or failed attempts.

Run it locally:

```bash
OLLAMA_EMBED_MODEL=mxbai-embed-large \
  .venv/bin/python benchmarks/token_savings.py
```

### 2. Agent-style context-gathering benchmark

`benchmarks/agent_context_benchmark.py` runs each task in two conditions:

- **Baseline.** A grep-agent simulation issues exact-token searches and
  returns matching line windows, repeated until the expected file is found
  or a budget is exhausted.
- **Treatment.** A single `mgrep search` call retrieves the top-k JSON
  snippets.

For both conditions the script counts:

- the size of the gathered context, in approximate tokens,
- whether the expected file appears in the gathered context,
- the number of tool calls used.

`estimated_total_token_reduction_x` adds a fixed grep-agent prompt-and-overhead
estimate to each side and reports the ratio. `context_token_reduction_x`
reports the ratio of context-only token volumes.

Run it locally:

```bash
OLLAMA_EMBED_MODEL=mxbai-embed-large \
  .venv/bin/python benchmarks/agent_context_benchmark.py
```

## Results across top-k

| top-k | recall (mgrep) | recall (grep) | estimated total-token reduction | context-token reduction | notes |
| --- | --- | --- | --- | --- | --- |
| 5 | 28 / 30 | 30 / 30 | 2.66× | 5.53× | Highest token efficiency; misses two expected files. |
| 10 | 30 / 30 | 30 / 30 | 2.00× | 2.90× | Equal recall to grep with the largest token reduction at parity. |
| 20 | 30 / 30 | 30 / 30 | 1.36× | 1.53× | Equal recall, smaller reduction. |
| 50 | 30 / 30 | 30 / 30 | 0.67× | 0.60× | Equal recall, but more tokens than the grep baseline. |

Top-k 10 is the only setting in this table where local-mgrep matches
grep-agent recall while keeping the estimated total-token reduction above 1×.

Reproduce a single row with:

```bash
.venv/bin/python benchmarks/agent_context_benchmark.py --top-k 10 --summary-only
```

## Limitations

The numbers above support narrow claims only.

- **Not provider billing.** No real coding agent is executed against a
  paid model in this benchmark. The total-token figure uses a fixed prompt
  and overhead model for the grep agent rather than a measured session.
- **Not an answer-quality evaluation.** Recall is "did the expected file
  appear in the gathered context", not "is the final synthesized answer
  correct against a rubric".
- **Repository-specific task set.** The 30 tasks are tied to this
  repository's structure and naming. Results on a different codebase may
  differ and should be measured on an independent task set.
- **Embedding model dependency.** The embedding model affects retrieval
  quality and changes the headline numbers. The published result uses
  `mxbai-embed-large`.
- **No cross-encoder rerank.** The lexical reranker uses simple token and
  phrase overlap. A second-stage reranker over a larger candidate pool is
  on the roadmap and would change the trade-off curve.

## Conditions for an end-to-end claim

A future end-to-end benchmark, suitable for the broader claim that
`local-mgrep` reduces token usage in real coding-agent sessions, requires
the following:

1. A task set of 30–50 questions or navigation tasks with expected files
   and rubric answers, including easy, medium, and multi-hop items.
2. The same model, repository commit, and system prompt across both
   conditions.
3. Per task and per condition, recorded measurements for input tokens,
   output tokens, tool-call/result tokens, number of tool calls,
   wall-clock latency, retrieval correctness, and a rubric-graded final
   answer score.
4. Two conditions:
   - **Baseline.** The agent may use exact search tools (`grep`, `rg`),
     file reads, and shell inspection, but not `local-mgrep`.
   - **Treatment.** The agent may issue one or more `mgrep search` calls
     before reading files and may verify with file reads afterwards.
5. The reported headline metric is
   `end_to_end_token_reduction = baseline total tokens / treatment total tokens`,
   reported alongside the rubric quality score on each side. A workflow
   that uses fewer tokens but produces a worse answer is worse, not better.

Until that benchmark is run, the claim from this repository is the narrower
one stated at the top of this document: equal recall at top-k 10 with about
a 2× estimated total-token reduction in a deterministic local
context-gathering benchmark on this codebase.
