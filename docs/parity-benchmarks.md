# Parity benchmarks

This document collects everything the repository currently knows about how
local-mgrep compares to alternative search tools. Each benchmark is
self-contained and reproducible from a fresh clone.

## Headline summary

| # | Comparison | Recall (mgrep / baseline) | Total-token reduction | Context-token reduction | Script |
|---|---|:---:|:---:|:---:|---|
| 1 | local-mgrep self-test vs **simulated grep-agent** | 30/30 vs 30/30 | **2.00×** | 2.90× | `benchmarks/agent_context_benchmark.py` |
| 2 | local-mgrep self-test vs **real ripgrep 15.1.0** | 30/30 vs 30/30 | **17.71×** | 32.80× | `benchmarks/parity_vs_ripgrep.py` |
| 3 | warp cross-repo vs **real ripgrep 15.1.0**, pre-P0 | 8/16 vs 16/16 | **868.6×** | 1256.98× | `benchmarks/parity_vs_ripgrep.py --tasks benchmarks/cross_repo/warp.json` |
| 3a | warp cross-repo, **after P0** (nomic-embed-text + cross-encoder rerank) | 9/16 vs 16/16 | **860.4×** | 1239.87× | same script with `--rerank` (default on) |
| 3b | warp cross-repo, **after P1** (P0 + chunk path/symbol prefix + chunker dedup) | 10/16 vs 16/16 | **528×** | 650× | `--rerank --reuse-index` after re-indexing |
| 3c | warp cross-repo, **after P2-F (HyDE)** mean across 3 runs | **12/16 mean** (11-13/16 range) vs 16/16 | **524×** | 644× | `--rerank --hyde --reuse-index`; LLM is non-deterministic |
| 3d | warp cross-repo, **after deterministic HyDE + `mxbai-rerank-large-v2`** | **14/16** vs 16/16 | ~525× | ~640× | same script with deterministic LLM seed and the larger reranker; 2 misses remain (websocket / billing) |
| 3e | warp cross-repo, **daily-driver mode** (`mxbai-rerank-base-v2`, no HyDE) | **10/16** vs 16/16 | ~525× | ~640× | the practical default: ~12.7 s avg per query on Mac CPU, no LLM call, base reranker only |

### Latency × recall trade-off curve on warp (Mac CPU, no daemon, cold reranker load amortised over 16 tasks)

Two retrieval modes: **chunk-only** (single-stage cosine over all chunks)
and **multi-resolution** (file-level cosine top-30 first, then chunk-level
within those 30 files). Multi-resolution is the default; disable with
`--no-multi-resolution`.

| Config | mode | recall | avg latency / query |
| --- | --- | :-: | :-: |
| raw cosine + lexical (no rerank, no HyDE) | chunk-only | 8/16 | 3.3 s |
| base rerank + no HyDE | chunk-only | 10/16 | 12.7 s |
| **base rerank + no HyDE + multi-resolution** ⭐ | multi-res | **10/16** | **8.1 s** |
| large rerank + no HyDE | chunk-only | 11/16 | 35.0 s |
| large rerank + no HyDE + multi-resolution | multi-res | 10/16 | 19.5 s |
| large rerank + HyDE | chunk-only | **14/16** | 54.1 s |
| **large rerank + HyDE + multi-resolution** | multi-res | **13/16** | **25.3 s** |
| large rerank + HyDE + multi-resolution (file-top 50) | multi-res | 13/16 | 25.3 s |
| ripgrep 15.1.0 raw recall | n/a | 16/16 | 0.1 s |

Multi-resolution makes every config ~2× faster while losing 0–1 / 16 recall.
The lost task is a query whose canonical file's chunk-mean vector lands
outside file-level top-50, so widening that pool does not help. The trade
is favourable: the daily-driver tier now lands at **10/16 @ 8.1 s**, the
accurate tier at **13/16 @ 25.3 s**, and the maximum-accuracy tier
(disable multi-res) at **14/16 @ 54 s**.

### Daemon mode (single-query latency)

The CLI loads the cross-encoder reranker on every short-lived ``mgrep
search`` invocation, paying ~5–10 s of model load per call. The new
``mgrep serve`` daemon eliminates that:

| Single query | latency |
| --- | :-: |
| in-process cold (load + inference) | 27.0 s |
| daemon warm (inference only) | 21.4 s |

The daemon saves the load step (~5–6 s) but the dominant cost is the
~20 s of cross-encoder inference itself on Mac CPU for 50 query/passage
pairs against the 2 B-parameter reranker. Daemon mode is an unambiguous
win for interactive multi-query usage (every subsequent query in the
session is the same warm cost) but does not by itself reach Mixedbread
cloud's ~250 ms latency — that gap is closed by reranker quantization,
not by removing the load step.

Use:

```
.venv/bin/mgrep serve --port 7878
.venv/bin/mgrep search "..." --daemon-url http://127.0.0.1:7878
```

The dominant cost above 3 s is the cross-encoder reranker on Mac CPU: large-v2
is ~2 B parameters and scores 50 query-passage pairs per query. Mixedbread's
cloud product runs the same reranker on A100 (0.89 s per query per their
model card); we lose this round to hardware, not algorithm. The next phases
on the roadmap (daemon-mode model retention, int8 quantization,
multi-resolution file-level retrieval) target this latency directly.
| 4 | local-mgrep vs **Mixedbread cloud mgrep** | not run (requires manual `mgrep login`) | n/a | n/a | `benchmarks/parity_vs_mixedbread.py` |

### P0–P2 ablation on warp (top-k = 10, pool = 50 unless noted)

| Config | mgrep recall | Notes |
| --- | :-: | --- |
| pre-P0: mxbai-embed-large + cosine + token-overlap lexical | 8/16 | benchmark #3 above |
| P0-B alone: nomic-embed-text + cosine + lexical, no rerank | 9/16 | embedding swap contributes +1 |
| P0-A + P0-B: nomic-embed-text + cross-encoder rerank, pool 50 | 9/16 | rerank contributes **0** at this pool size |
| P0-A + P0-B with rerank pool 200 | 9/16 | wider pool does not surface missing answers |
| P0 + P1-C + P1-E (chunk path/symbol prefix + tree-sitter dedup, max_chars 2000) | **10/16** | path tokens now visible to embedder |
| P0 + P1 + P2-F (HyDE), 3 runs | **11 / 12 / 13** | LLM-driven hypothetical doc; mean ≈ 12/16, best 13/16 |
| above + deterministic HyDE seed (qwen2.5:3b, temperature 0) | **11/16** stable | non-determinism removed; reproducible runs |
| above + `mxbai-rerank-large-v2` (Mixedbread cloud's flagship reranker) | **14/16** stable | +3 over deterministic base reranker; same model the cloud product uses |
| above + non-canonical-path penalty (`/blocklist/`, `_test.rs`, etc., × 0.5) | 14/16 | no measurable lift on warp's task set; kept in code as a small principled guard |

**Reading the P0 row.** The rerank stage cannot help when the right file is
not in the top-N cosine candidate pool to begin with. Inspecting the failing
queries shows the question is phrased in user-language ("microphone audio
captured", "subscription tier checked") while the chunk body uses
code-vocabulary (`AudioInput`, `TierGuard`) with no surface overlap. The
path / filename carries the only word-level bridge — and the embedder never
sees it because the chunk text we store is the raw code only.

**Reading the P1 row.** Once each chunk's text is prefixed with
`[file: …] [lang: …] [symbol: …]`, the embedder finally has a path-token
anchor and recall lifts to 10/16. The chunker dedup change halves the
indexed-chunk count (53 382 → 26 454 on warp) without losing recall, because
emit-and-stop on the largest fitting node prevents redundant nested chunks.

**Reading the P2 row.** HyDE generates a short hypothetical code answer with
the local LLM (qwen2.5:3b) and combines it with the original question before
embedding. On warp this lifts mean recall to ~12/16 (best 13/16) but introduces
non-determinism — the same query can land in a different top-10 across runs
because the hypothetical doc varies. Switching to a deterministic LLM seed
or a larger code-aware reasoning model would tighten this.

All benchmarks fix top-k = 10, embedding model = `mxbai-embed-large` (Ollama),
and chars-per-token = 4. Token reductions are
`baseline_total_tokens / mgrep_total_tokens`. Recall is "did the expected file
appear in the top-k results", measured as a substring match against the
returned paths so directory-level expectations (e.g. `crates/ai/`) hit any
file inside.

## 1 — Self-test vs simulated grep agent

**Source:** `benchmarks/agent_context_benchmark.py`

The simulated grep agent extracts up to 8 token variants from each question,
then for each term scans indexed files in Python and stops once a per-term
match cap is reached. Results are accumulated until either the cap is hit or
the file list is exhausted.

```bash
.venv/bin/python benchmarks/agent_context_benchmark.py --top-k 10 --summary-only
```

**Result (top-k = 10, on the local-mgrep repo):**

| Metric | Simulated grep | local-mgrep |
| --- | :-: | :-: |
| Expected-file recall | 30 / 30 | 30 / 30 |
| Tool calls | 227 | 30 |
| Context tokens | summed over 30 tasks | ÷ 2.90 |
| Estimated total tokens (incl. fixed prompt + answer overhead) | summed | ÷ 2.00 |

**Limit:** the simulated agent's per-term break logic understates how much
context a real `rg`-driven agent actually pulls. Use benchmark #2 for the
realistic ceiling.

## 2 — Self-test vs real ripgrep

**Source:** `benchmarks/parity_vs_ripgrep.py`

Replaces the Python simulation with actual `rg --json -F -i -C 2 TERM ROOT`
calls, one per extracted term. Output is parsed from ripgrep's JSON event
stream so per-file `--max-count` accumulates across the repository the way it
would in a real agent loop.

```bash
.venv/bin/python benchmarks/parity_vs_ripgrep.py --top-k 10 --summary-only
```

**Result (top-k = 10, on the local-mgrep repo, ripgrep 15.1.0):**

| Metric | ripgrep 15.1.0 | local-mgrep | mgrep advantage |
| --- | :-: | :-: | :-: |
| Expected-file recall | 30 / 30 | 30 / 30 | tied |
| Context tokens | 1,416,412 | 43,185 | **32.8× less** |
| Estimated total tokens | 1,455,412 | 82,185 | **17.7× less** |
| Tool calls | 227 | 30 | 7.6× fewer |
| Avg latency / task | 0.100 s | 0.037 s | ~3× faster |
| Indexing time (one-shot) | n/a | 6.7 s | — |

**Reading:** at the same recall, a coding agent that uses ripgrep for context
gathering on this repository pulls **17.7× more total tokens** than the same
agent using local-mgrep. The 32.8× context-only ratio shows the upper bound
once fixed prompt and answer overhead are removed.

This is the most direct answer to the question "is local-mgrep better than
running rg locally?" for the agent-context use case on this codebase.

## 3 — Cross-repo on warp (real ripgrep)

**Source:** `benchmarks/parity_vs_ripgrep.py` + `benchmarks/cross_repo/warp.json`

To rule out the "tasks are tied to local-mgrep" critique, the same benchmark
is run against the [warp](https://github.com/warpdotdev/warp) terminal source
(~3.2k Rust files, 65+ crates) with a hand-curated 16-task set covering AI
integration, computer-use, editor, LSP, vim mode, voice input, completion,
fuzzy match, settings, websockets, secrets, markdown rendering, command
palette, auth, billing, and code review.

```bash
.venv/bin/python benchmarks/parity_vs_ripgrep.py \
  --root /path/to/warp \
  --tasks benchmarks/cross_repo/warp.json \
  --top-k 10 --summary-only
```

**Result (top-k = 10, on the warp Rust workspace, ripgrep 15.1.0):**

| Metric | ripgrep 15.1.0 | local-mgrep | Notes |
| --- | :-: | :-: | :-: |
| Expected-file recall | 16 / 16 | **8 / 16** | rg finds all in raw output; mgrep recovers half |
| Context tokens | 58,405,476 | 46,465 | mgrep 1,257× less |
| Estimated total tokens | 58,426,276 | 67,265 | mgrep 869× less |
| Tool calls | 127 | 16 | rg 8× more |
| Avg latency / task | 3.515 s | 2.078 s | mgrep 1.7× faster |
| Indexing time (one-shot, 3,173 files / 53,382 chunks) | n/a | ~40 min | Ollama embedding throughput on Mac |

**Reading — and this is the most honest data point in this document:**

On a large unfamiliar Rust codebase (warp, 65+ crates, ~3.2k files, ~46M
chars / ~12M approx tokens), the trade-off is real and asymmetric:

- **ripgrep recovers every expected file in raw output** but produces
  ~58M tokens of context across the 16 tasks — that exceeds any LLM's
  practical context window. An agent cannot actually feed all of that
  to a model; it has to truncate aggressively, which deletes most of
  the recall.
- **local-mgrep keeps context to ~46k tokens** (1,257× smaller) but
  only places the expected directory inside the top-10 for **8 of 16
  tasks**. The other 8 questions are answered in the corpus but the
  semantic ranker did not surface their expected directory in time.

The ~50% recall on warp is materially worse than the 30/30 on the
self-test. The likely contributors, in order:

1. **Embedding model.** `mxbai-embed-large` (1024-d, 512-token context)
   was trained on general English; semantic distinguishing-power on
   dense Rust code is bounded.
2. **No cross-encoder rerank.** The hybrid score is just
   `0.8 · cosine + 0.2 · token-overlap`; a second-stage reranker over a
   wider candidate pool (e.g. top-100 from cosine, then a small
   cross-encoder) would likely lift recall meaningfully on this kind
   of corpus. It is on the roadmap.
3. **Per-file diversification cap.** With `MAX_RESULTS_PER_FILE = 2`,
   when several relevant chunks live in the same `lib.rs`, only the
   top 2 surface; for sparsely-distributed evidence this is fine, for
   "the answer is concentrated in one big file" it can hurt.
4. **Top-k = 10.** Increasing k would directly raise recall at the
   cost of more output tokens. A higher-k run is on the roadmap.

The bottom line is the pair of numbers above: **869× fewer tokens, 50%
of the expected hits**. That is real data, not parity. mgrep on this
hardware and embedding model is not a drop-in replacement for ripgrep
when recall is the bar; it is a drop-in replacement for "an agent
naively dumping `rg`-output into an LLM" when context budget matters
more than perfect recall, with the open improvements above as the path
to closing the recall gap.

The full task-level breakdown is in the JSON report (drop the
`--summary-only` flag); 2 chunks out of 53,382 produced an embedding
error and were stored as zero vectors (logged as warnings). Those zero
vectors do not match real queries and contribute to the recall miss
slightly but are not the dominant cause.

## 4 — Mixedbread cloud parity (not run)

**Source:** `benchmarks/parity_vs_mixedbread.py`

The original `@mixedbread/mgrep` is a cloud product that uploads the
repository to Mixedbread's servers and serves embeddings + reranking from
their hosted models. There is no offline mode, and the CLI requires an
interactive `mgrep login` OAuth flow against `mixedbread.com`. Free-tier
quotas apply.

This repository's `parity_vs_mixedbread.py` provides a runnable harness that
shells out to the Mixedbread CLI, parses its stdout into paths, and compares
hit rate / latency / parsed-content tokens against local-mgrep on the same
task list. It explicitly refuses to use `/opt/homebrew/bin/mgrep` if that
resolves to this repository's own wrapper.

**Why it is not run here:**

1. The benchmark requires an interactive OAuth login that cannot run in a
   non-interactive setting.
2. Running it uploads the target repository to Mixedbread's cloud — for
   private codebases this is a non-trivial disclosure decision and should
   be made by a human operator.

When a Mixedbread account is available, the script in this repository runs
end-to-end with one `mgrep search` per task; see the script's docstring for
the one-time setup steps.

**What such a parity run would tell us that the other benchmarks cannot:**
how much retrieval quality is given up by switching from a paid cloud
embedding model (Mixedbread's hosted embedder + reranker) to a local one
(Ollama `mxbai-embed-large`, no reranker yet). Until that run exists, this
repository makes **no claim** of accuracy parity with the original cloud
mgrep.

## Closing the recall gap — see the roadmap

The 50% warp recall gap above is being addressed by the structured plan in
[`docs/roadmap.md`](roadmap.md). The first phase (P0) introduces a
cross-encoder reranker as a second-stage scorer and switches the default
embedding model to `nomic-embed-text`. Subsequent phases add path / symbol
chunk prefixes (P1-C), BM25 lexical with column weighting (P1-D), tree-sitter
chunker dedup (P1-E), HyDE for natural-language queries (P2-F), and an
asymmetric query / passage prefix lookup (P2-G).

Each phase appends a new row to the headline table above so the recall and
token-reduction trade-off curve is visible in version control.

## Limitations and what is intentionally not measured

The numbers above support narrow claims only.

- **Not provider billing.** No real coding agent is executed against a paid
  model in any of these benchmarks. The total-token figures use a fixed
  prompt-and-overhead estimate for the baseline agent rather than a measured
  session.
- **Not an answer-quality evaluation.** Hit rate is "did the expected file
  appear in the gathered context", not "is the final synthesized answer
  correct against a rubric".
- **Embedding-model dependent.** Switching `OLLAMA_EMBED_MODEL` changes the
  retrieval numbers. Published results use `mxbai-embed-large`.
- **Hand-curated task sets.** Both the 30-task self-test and the 16-task
  warp set were authored by hand. Generalization to arbitrary repositories
  requires a broader independent task set.
- **No cross-encoder rerank.** The lexical reranker uses simple token and
  phrase overlap. A second-stage reranker over a larger candidate pool is
  on the roadmap and would change the trade-off curve.

## What an end-to-end agent claim would still need

A future end-to-end benchmark, suitable for the broader claim that local-mgrep
reduces token usage in real coding-agent sessions, requires the following:

1. A task set of 30–50 questions with expected files and rubric answers,
   covering easy, medium, and multi-hop items, on at least three different
   repositories.
2. The same model, repository commit, and system prompt across both
   conditions.
3. Per task and per condition, recorded measurements for input tokens,
   output tokens, tool-call/result tokens, number of tool calls, wall-clock
   latency, retrieval correctness, and a rubric-graded final answer score.
4. Two conditions:
   - **Baseline.** The agent may use exact search tools (`grep`, `rg`),
     file reads, and shell inspection, but not local-mgrep.
   - **Treatment.** The agent may issue one or more `mgrep search` calls
     before reading files and may verify with file reads afterwards.
5. The reported headline metric is
   `end_to_end_token_reduction = baseline total tokens / treatment total tokens`,
   reported alongside the rubric quality score on each side. A workflow that
   uses fewer tokens but produces a worse answer is worse, not better.

Until that benchmark is run, the strongest claim from this repository is the
one in benchmark #2 above: equal recall at top-k 10 with about a 17.7×
estimated total-token reduction in a deterministic local context-gathering
benchmark on this codebase against a real `rg` baseline.
