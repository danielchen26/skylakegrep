# Token Benchmarking

`local-mgrep` can be evaluated at two different levels. Keeping these separate
prevents over-claiming.

## 1. Retrieval-layer context compression

This is what `benchmarks/token_savings.py` measures.

It compares:

1. **Whole indexed corpus tokens** — approximate tokens for every file that
   `local-mgrep` indexes.
2. **Whole source+docs tokens** — approximate tokens for source, docs, and
   project metadata that an agent might otherwise read.
3. **Retrieved context tokens** — approximate tokens in the top-k JSON snippets
   returned by `local-mgrep search`.

The reported ratios are:

```text
indexed_context_reduction_x = indexed corpus tokens / retrieved JSON tokens
source_doc_context_reduction_x = source+docs tokens / retrieved JSON tokens
```

These numbers answer: **how much smaller is the context we hand to an LLM after
semantic retrieval?** They do not measure full Claude/OpenCode/Codex session
usage, because they do not count planning prompts, tool-call wrappers, repeated
searches, file reads, final answers, or failed attempts.

Run it locally:

```bash
OLLAMA_EMBED_MODEL=mxbai-embed-large .venv/bin/python benchmarks/token_savings.py
```

The default benchmark uses a small query set with expected source files. It
reports both compression and whether the expected file appears in the top-k
results.

## 2. Original-mgrep-like agent token benchmark

The original hosted `mgrep` claim is an end-to-end coding-agent claim: compare a
grep-based agent workflow with an mgrep-assisted agent workflow over many tasks,
then count total token usage and quality.

A local equivalent should use this protocol:

For a deterministic local approximation, run:

```bash
OLLAMA_EMBED_MODEL=mxbai-embed-large .venv/bin/python benchmarks/agent_context_benchmark.py
```

This script compares a grep-like context-gathering agent against a
local-mgrep-assisted context-gathering agent over a fixed task set. It reports
context-only token reduction and an estimated full-agent ratio after adding fixed
prompt/output overhead. It is still not a provider billing benchmark because it
does not execute Claude/OpenCode/Codex, but it is much closer to the original
claim than retrieval compression alone.

To compare recall/efficiency tradeoffs quickly:

```bash
.venv/bin/python benchmarks/agent_context_benchmark.py --top-k 5 --summary-only
.venv/bin/python benchmarks/agent_context_benchmark.py --top-k 10 --summary-only
.venv/bin/python benchmarks/agent_context_benchmark.py --top-k 20 --summary-only
```

Current local benchmark snapshot on this repository:

| mgrep top-k | mgrep hit rate | Estimated total token reduction | Context token reduction | Notes |
| --- | --- | --- | --- | --- |
| 5 | 28/30 | 2.66x | 5.53x | Very token efficient; still misses two expected files. |
| 10 | 30/30 | 2.00x | 2.90x | Best current default: equal expected-file recall to grep with about 2x estimated total-token reduction. |
| 20 | 30/30 | 1.36x | 1.53x | Full recall with lower savings than top-k 10. |
| 50 | 30/30 | 0.67x | 0.60x | Full recall, but more tokens than grep; not useful as an efficiency default. |

The current honest conclusion is: with local per-file result diversification,
local-mgrep demonstrates original-like token reduction at top-k 10 while matching
grep's expected-file recall on this deterministic task set. This is still not a
provider billing benchmark or a full answer-quality rubric.

## 3. Closing the local quality gap

Remaining gaps before making a broader original-mgrep-style claim:

1. **No cross-encoder/cloud-grade reranker.** The first semantic retrieval pass
   can rank semantically adjacent files above the exact expected file. A local
   replacement should add a second-stage reranker that scores `(query, snippet)`
   pairs after vector retrieval. Candidate local approaches:
   - a lightweight lexical/BM25 score blended with vector similarity,
   - a local Ollama rerank prompt over the top 20-50 snippets,
   - or a small local cross-encoder if a suitable open model is available.
2. **Agent integration policy is still missing.** Original mgrep benefits from
   being installed into coding agents. A local plugin should teach agents when to call
   `local-mgrep`, when to follow up with file reads, and when to fall back to
   exact grep.

The earlier weak result-diversification gap is now addressed with a
local per-file cap before final top-k output. That keeps high-scoring chunks while
avoiding context waste from repeated same-file results.

Recommended implementation order:

1. **Diversity layer** — done locally with a default per-file cap before final
   output.
2. **Local hybrid reranker** — extend current lexical+semantic scoring
   into a configurable reranking pipeline over a larger candidate pool.
3. **Agent integration** — provide OpenCode/Claude/Codex instructions or
   plugin installers that force a high-recall workflow: `mgrep search -> read the
   top files -> answer with citations`.
4. **Full 50-task benchmark** — once recall matches grep at useful top-k,
   run the benchmark with an actual agent and provider token accounting.

Success criteria for claiming parity with the original-style token result:

```text
mgrep hit rate >= grep hit rate - 1 task
estimated_total_token_reduction_x >= 2.0
quality rubric score >= grep baseline
```

Until then, the correct claim is narrower: local-mgrep has demonstrated local
retrieval compression and a deterministic agent-context benchmark showing about
2x estimated token reduction at top-k 10 with 30/30 expected-file recall.

### Task set

- Use 30-50 repository questions or navigation tasks.
- Each task should have an expected file or answer rubric.
- Include easy, medium, and multi-hop tasks.
- Do not include benchmark harness source files in the indexed corpus.

Example task shape:

```json
{
  "id": "hybrid-ranking-001",
  "question": "Where are lexical and semantic scores combined?",
  "expected_files": ["local_mgrep/src/storage.py"],
  "rubric": "Answer should mention combine_scores and query_text-gated lexical boosting."
}
```

### Conditions

Run each task under the same model, repository commit, and system prompt.

1. **Baseline grep condition**
   - Agent may use exact search tools such as `grep`, `rg`, file reads, and shell
     inspection.
   - Agent may not use `local-mgrep`.

2. **Treatment local-mgrep condition**
   - Agent may use `local-mgrep search` first.
   - Agent may still read files after retrieval for verification.
   - Agent should not read the whole repository unless retrieval fails.

### Measurements

For every task and condition, record:

- Input tokens
- Output tokens
- Tool-call/result tokens
- Number of tool calls
- Wall-clock latency
- Whether expected files were found
- Final answer quality score from the rubric

The headline metric matching the original claim is:

```text
end_to_end_token_reduction = baseline total tokens / treatment total tokens
```

Quality must be reported beside token reduction. A workflow that uses fewer
tokens but misses the correct file is worse, not better.

### Expected relationship to retrieval compression

Retrieval-layer compression can be much larger than end-to-end savings. For
example, reducing context from 24k tokens to 700 retrieved tokens is a large
context reduction, but the final agent workflow still includes prompts, tool
metadata, follow-up file reads, and the final response. A 20x retrieval
compression may become a much smaller end-to-end savings ratio.

That is why `benchmarks/token_savings.py` is a useful lower-level signal, while a
50-task agent benchmark is needed for an original-mgrep-style headline claim.
