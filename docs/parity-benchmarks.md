# Parity benchmarks

This document collects everything the repository currently knows about how
skylakegrep compares to alternative search tools. Each benchmark is
self-contained and reproducible from a fresh clone.

## Headline summary

| # | Comparison | Recall (skygrep / baseline) | Total-token reduction | Context-token reduction | Script |
|---|---|:---:|:---:|:---:|---|
| 1 | skylakegrep self-test vs **simulated grep-agent** | 30/30 vs 30/30 | **2.00×** | 2.90× | `benchmarks/agent_context_benchmark.py` |
| 2 | skylakegrep self-test vs **real ripgrep 15.1.0** | 30/30 vs 30/30 | **17.71×** | 32.80× | `benchmarks/parity_vs_ripgrep.py` |
| 3 | Rust workspace cross-repo vs **real ripgrep 15.1.0**, pre-P0 | 8/16 vs 16/16 | **868.6×** | 1256.98× | `benchmarks/parity_vs_ripgrep.py --tasks benchmarks/cross_repo/rust-workspace.json` |
| 3a | Rust workspace cross-repo, **after P0** (nomic-embed-text + cross-encoder rerank) | 9/16 vs 16/16 | **860.4×** | 1239.87× | same script with `--rerank` (default on) |
| 3b | Rust workspace cross-repo, **after P1** (P0 + chunk path/symbol prefix + chunker dedup) | 10/16 vs 16/16 | **528×** | 650× | `--rerank --reuse-index` after re-indexing |
| 3c | Rust workspace cross-repo, **after P2-F (HyDE)** mean across 3 runs | **12/16 mean** (11-13/16 range) vs 16/16 | **524×** | 644× | `--rerank --hyde --reuse-index`; LLM is non-deterministic |
| 3d | Rust workspace cross-repo, **after deterministic HyDE + `mxbai-rerank-large-v2`** | **14/16** vs 16/16 | ~525× | ~640× | same script with deterministic LLM seed and the larger reranker; 2 misses remain (websocket / billing) |
| 3e | Rust workspace cross-repo, **daily-driver mode** (`mxbai-rerank-base-v2`, no HyDE) | **10/16** vs 16/16 | ~525× | ~640× | the practical default: ~12.7 s avg per query on Mac CPU, no LLM call, base reranker only |
| 3f | Rust workspace cross-repo, **confidence-gated cascade** (`skygrep search --cascade`, τ=0.015) | **14/16** vs 16/16 | ~525× | ~640× | file-mean cosine first, escalate to HyDE-union only on uncertain queries — **1.49 s avg per query** (14× faster than tier 3d at the same recall) |

### Latency × recall trade-off curve on Rust workspace (Mac CPU, no daemon, cold reranker load amortised over 16 tasks)

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

### Confidence-gated cascade (the new max-accurate tier)

`skygrep search --cascade` (added 2026-05-03) replaces the previous
"max-accurate" config (`--rerank --hyde --rank-by file`, 14/16 @ 21.8 s)
with a confidence-gated retrieval that only pays the LLM-driven escalation
on queries the cheap path is uncertain about. Empirically on Rust workspace:

| Config (rg prefilter on, ``--cascade``, varying τ) | recall | avg latency / query | early-exit % |
| --- | :-: | :-: | :-: |
| τ = 0.000 (always cheap) | 11/16 | **0.10 s** | 100% |
| τ = 0.005 | 12/16 | 0.78 s | 56% |
| τ = 0.010 | 13/16 | 1.10 s | 38% |
| **τ = 0.015 (default)** ⭐ | **14/16** | **1.49 s** | 19% |
| τ = 0.020 | 14/16 | 1.65 s | 13% |
| τ = 0.030 | 14/16 | 1.89 s | 6% |

Cheap path: file-mean cosine on the lexical-prefilter candidates → top
file-rank chunks for those files. Latency dominated by ripgrep + a single
embedding call.

Escalation: Round A (`cosine + file-rank`) ∪ Round C (`HyDE + cosine +
file-rank`), score-preserving dedup, top-K. The LLM is consulted **only**
on queries where the cheap path's top-1 score doesn't dominate top-2 by
the threshold.

The same 2 misses survive (`crates/ai/`, `app/src/billing/`) — these are
hard semantic disambiguation cases where the canonical file's surface
vocabulary doesn't overlap the question's, and even multi-HyDE union
doesn't break them. See [`docs/roadmap.md`](./roadmap.md) §P4.

The cascade is opt-in: pass `--cascade` to enable it, with `--cascade-tau`
to tune the threshold. The non-cascade defaults are unchanged.

### Lexical prefilter (the new default first stage)

The architecture was redrawn so that ripgrep is now the **first** stage of
the pipeline, not just a benchmark baseline. ``skygrep search`` extracts up
to 8 literal tokens from the query, asks ``rg -il -F`` for files
containing any of them, and restricts the cosine + rerank stages to chunks
of those files only. Empirically on Rust workspace:

| Config (with ``--lexical-prefilter`` on, multi-resolution intersected, ``--rank-by file``) | recall | avg latency / query |
| --- | :-: | :-: |
| **cosine + no rerank** | **9/16** | **0.52 s** ⭐ daily-driver |
| cosine + ``mxbai-rerank-base-v2`` | **11/16** | **9.5 s** |
| **cosine + ``mxbai-rerank-large-v2`` + HyDE** | **14/16** | **21.8 s** ⭐ max recall |

The ``--rank-by file`` flag (added 2026-05-03) collapses the result list so
each candidate file contributes exactly one slot — its highest-scoring
chunk. This stops large consumer files from drowning out small canonical
files at the chunk stage, and unlocks the **14/16 ceiling at 21.8 s**: the
same peak recall the chunk-only ``no-MR + no-prefilter + HyDE`` config
required 54 s to reach. With ``--rank-by chunk`` (the legacy default), the
same config still tops out at 13/16.

For comparison, the old chunk-only architecture (no prefilter) on the same
machine and index hit:

| Config (no prefilter) | recall | avg latency / query |
| --- | :-: | :-: |
| multi-resolution + base + no HyDE | 10/16 | 8.1 s |
| multi-resolution + large + HyDE | 13/16 | 25.3 s |
| chunk-only + large + HyDE | 14 / 16 | 54 s |

**The big win is the daily-driver tier dropping from 8 s to 0.5 s (16×
faster) at one fewer recall point**. For workflows that do many short
queries against a large index, this is the relevant change. The
maximum-accuracy tier still pays cross-encoder + LLM time and stays at
13–14 / 16; the prefilter doesn't free those configs much because the
remaining cost is rerank inference, not corpus scan.

**Why the prefilter doesn't reach 16/16.** ripgrep alone in raw output
already gets 16/16 file-set membership (its 0.43 s is the recall ceiling
on this task set), but the search pipeline still has to compress that
file set down to 10 chunk-level results, and on some queries the
canonical file's chunks rank below other rg-candidate chunks under
cosine. This is a chunk-ranking issue inside the prefilter set, not a
prefilter recall failure. Closing it requires either reranking the file
set instead of chunks (so each canonical file gets at least one slot) or
using BM25 over the prefilter set rather than cosine.

The prefilter is on by default (``--no-lexical-prefilter`` to disable);
the candidate root defaults to the working directory and can be set with
``--lexical-root``. When ripgrep returns fewer than ``--lexical-min-candidates``
files (default 2), the search falls back to corpus-wide cosine — the
escape hatch for queries with no usable surface-level overlap.

### Quantisation and device probe (Mac CPU / MPS)

We added ``SKYGREP_RERANK_QUANTIZE=int8`` (torch dynamic quantisation of
the cross-encoder Linear layers) and ``SKYGREP_RERANK_DEVICE=auto/mps/cpu``
as knobs, then measured cold single-query latency for the
2 B-parameter ``mxbai-rerank-large-v2`` model on this Mac:

| Knob | cold single-query |
| --- | :-: |
| fp32 + cpu (baseline) | 27.0 s |
| int8 dynamic quantisation | 28.1 s (**no improvement**) |
| MPS (Apple GPU) | 27.1 s (**no improvement**) |
| **switch to ``mxbai-rerank-base-v2`` (0.5 B params, fp32 + cpu)** | **14.4 s** |

Two negative findings worth recording so future work doesn't repeat them:

- ``torch.quantization.quantize_dynamic`` is optimised for x86_64 CPUs
 with VNNI instructions. Apple Silicon CPUs do not have those, so the
 quantised int8 kernels fall back to fp32-equivalent paths and we see no
 net win. Quantisation is therefore left as an opt-in for x86_64
 deployments via ``SKYGREP_RERANK_QUANTIZE=int8``.
- MPS support in PyTorch is per-op; the Qwen2 cross-encoder used by
 ``mxbai-rerank-large-v2`` includes ops without fast MPS kernels, which
 forces tensors to round-trip to CPU per-layer. The result on this
 hardware is no net speedup. ``SKYGREP_RERANK_DEVICE=auto`` (the default)
 still picks MPS when available so we benefit on architectures or future
 PyTorch versions where this is fixed.

The lever that *does* shrink reranker latency on this hardware is the
**model itself**: switching from large to base (3× fewer parameters) is a
~2× cold-query speedup with the trade-offs documented in the table above
(11/16 → 10/16 chunk-only, 13/16 → 10/16 multi-resolution).

### Daemon mode (single-query latency)

The CLI loads the cross-encoder reranker on every short-lived ``skygrep
search`` invocation, paying ~5–10 s of model load per call. The new
``skygrep serve`` daemon eliminates that:

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
.venv/bin/skygrep serve --port 7878
.venv/bin/skygrep search "..." --daemon-url http://127.0.0.1:7878
```

The dominant cost above 3 s is the cross-encoder reranker on Mac CPU: large-v2
is ~2 B parameters and scores 50 query-passage pairs per query. Mixedbread's
cloud product runs the same reranker on A100 (0.89 s per query per their
model card); we lose this round to hardware, not algorithm. The next phases
on the roadmap (daemon-mode model retention, int8 quantization,
multi-resolution file-level retrieval) target this latency directly.
| 4 | skylakegrep vs **Mixedbread cloud skygrep** | not run (requires manual `skygrep login`) | n/a | n/a | `benchmarks/parity_vs_mixedbread.py` |

### P0–P2 ablation on Rust workspace (top-k = 10, pool = 50 unless noted)

| Config | skygrep recall | Notes |
| --- | :-: | --- |
| pre-P0: mxbai-embed-large + cosine + token-overlap lexical | 8/16 | benchmark #3 above |
| P0-B alone: nomic-embed-text + cosine + lexical, no rerank | 9/16 | embedding swap contributes +1 |
| P0-A + P0-B: nomic-embed-text + cross-encoder rerank, pool 50 | 9/16 | rerank contributes **0** at this pool size |
| P0-A + P0-B with rerank pool 200 | 9/16 | wider pool does not surface missing answers |
| P0 + P1-C + P1-E (chunk path/symbol prefix + tree-sitter dedup, max_chars 2000) | **10/16** | path tokens now visible to embedder |
| P0 + P1 + P2-F (HyDE), 3 runs | **11 / 12 / 13** | LLM-driven hypothetical doc; mean ≈ 12/16, best 13/16 |
| above + deterministic HyDE seed (qwen2.5:3b, temperature 0) | **11/16** stable | non-determinism removed; reproducible runs |
| above + `mxbai-rerank-large-v2` (Mixedbread cloud's flagship reranker) | **14/16** stable | +3 over deterministic base reranker; same model the cloud product uses |
| above + non-canonical-path penalty (`/blocklist/`, `_test.rs`, etc., × 0.5) | 14/16 | no measurable lift on Rust workspace's task set; kept in code as a small principled guard |

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
indexed-chunk count (53 382 → 26 454 on Rust workspace) without losing recall, because
emit-and-stop on the largest fitting node prevents redundant nested chunks.

**Reading the P2 row.** HyDE generates a short hypothetical code answer with
the local LLM (qwen2.5:3b) and combines it with the original question before
embedding. On Rust workspace this lifts mean recall to ~12/16 (best 13/16) but introduces
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

**Result (top-k = 10, on the skylakegrep repo):**

| Metric | Simulated grep | skylakegrep |
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

**Result (top-k = 10, on the skylakegrep repo, ripgrep 15.1.0):**

| Metric | ripgrep 15.1.0 | skylakegrep | skygrep advantage |
| --- | :-: | :-: | :-: |
| Expected-file recall | 30 / 30 | 30 / 30 | tied |
| Context tokens | 1,416,412 | 43,185 | **32.8× less** |
| Estimated total tokens | 1,455,412 | 82,185 | **17.7× less** |
| Tool calls | 227 | 30 | 7.6× fewer |
| Avg latency / task | 0.100 s | 0.037 s | ~3× faster |
| Indexing time (one-shot) | n/a | 6.7 s | — |

**Reading:** at the same recall, a coding agent that uses ripgrep for context
gathering on this repository pulls **17.7× more total tokens** than the same
agent using skylakegrep. The 32.8× context-only ratio shows the upper bound
once fixed prompt and answer overhead are removed.

This is the most direct answer to the question "is skylakegrep better than
running rg locally?" for the agent-context use case on this codebase.

## 3 — Cross-repo on Rust workspace (real ripgrep)

**Source:** `benchmarks/parity_vs_ripgrep.py` + `benchmarks/cross_repo/rust-workspace.json`

To rule out the "tasks are tied to skylakegrep" critique, the same benchmark
is run against the [Rust workspace](the Rust terminal source tree (URL redacted)) terminal source
(~3.2k Rust files, 65+ crates) with a hand-curated 16-task set covering AI
integration, computer-use, editor, LSP, vim mode, voice input, completion,
fuzzy match, settings, websockets, secrets, markdown rendering, command
palette, auth, billing, and code review.

```bash
.venv/bin/python benchmarks/parity_vs_ripgrep.py \
 --root /path/to/Rust workspace \
 --tasks benchmarks/cross_repo/rust-workspace.json \
 --top-k 10 --summary-only
```

**Result (top-k = 10, on the Rust workspace Rust workspace, ripgrep 15.1.0):**

| Metric | ripgrep 15.1.0 | skylakegrep | Notes |
| --- | :-: | :-: | :-: |
| Expected-file recall | 16 / 16 | **8 / 16** | rg finds all in raw output; skygrep recovers half |
| Context tokens | 58,405,476 | 46,465 | skygrep 1,257× less |
| Estimated total tokens | 58,426,276 | 67,265 | skygrep 869× less |
| Tool calls | 127 | 16 | rg 8× more |
| Avg latency / task | 3.515 s | 2.078 s | skygrep 1.7× faster |
| Indexing time (one-shot, 3,173 files / 53,382 chunks) | n/a | ~40 min | Ollama embedding throughput on Mac |

**Reading — and this is the most honest data point in this document:**

On a large unfamiliar Rust codebase (Rust workspace, 65+ crates, ~3.2k files, ~46M
chars / ~12M approx tokens), the trade-off is real and asymmetric:

- **ripgrep recovers every expected file in raw output** but produces
 ~58M tokens of context across the 16 tasks — that exceeds any LLM's
 practical context window. An agent cannot actually feed all of that
 to a model; it has to truncate aggressively, which deletes most of
 the recall.
- **skylakegrep keeps context to ~46k tokens** (1,257× smaller) but
 only places the expected directory inside the top-10 for **8 of 16
 tasks**. The other 8 questions are answered in the corpus but the
 semantic ranker did not surface their expected directory in time.

The ~50% recall on Rust workspace is materially worse than the 30/30 on the
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
of the expected hits**. That is real data, not parity. skygrep on this
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

The original `@mixedbread/skygrep` is a cloud product that uploads the
repository to Mixedbread's servers and serves embeddings + reranking from
their hosted models. There is no offline mode, and the CLI requires an
interactive `skygrep login` OAuth flow against `mixedbread.com`. Free-tier
quotas apply.

This repository's `parity_vs_mixedbread.py` provides a runnable harness that
shells out to the Mixedbread CLI, parses its stdout into paths, and compares
hit rate / latency / parsed-content tokens against skylakegrep on the same
task list. It explicitly refuses to use `/opt/homebrew/bin/skygrep` if that
resolves to this repository's own wrapper.

**Why it is not run here:**

1. The benchmark requires an interactive OAuth login that cannot run in a
 non-interactive setting.
2. Running it uploads the target repository to Mixedbread's cloud — for
 private codebases this is a non-trivial disclosure decision and should
 be made by a human operator.

When a Mixedbread account is available, the script in this repository runs
end-to-end with one `skygrep search` per task; see the script's docstring for
the one-time setup steps.

**What such a parity run would tell us that the other benchmarks cannot:**
how much retrieval quality is given up by switching from a paid cloud
embedding model (Mixedbread's hosted embedder + reranker) to a local one
(Ollama `mxbai-embed-large`, no reranker yet). Until that run exists, this
repository makes **no claim** of accuracy parity with the original cloud
skygrep.

## Closing the recall gap — see the roadmap

The 50% Rust workspace recall gap above is being addressed by the structured plan in
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
 Rust workspace set were authored by hand. Generalization to arbitrary repositories
 requires a broader independent task set.
- **No cross-encoder rerank.** The lexical reranker uses simple token and
 phrase overlap. A second-stage reranker over a larger candidate pool is
 on the roadmap and would change the trade-off curve.

## What an end-to-end agent claim would still need

A future end-to-end benchmark, suitable for the broader claim that skylakegrep
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
 file reads, and shell inspection, but not skylakegrep.
 - **Treatment.** The agent may issue one or more `skygrep search` calls
 before reading files and may verify with file reads afterwards.
5. The reported headline metric is
 `end_to_end_token_reduction = baseline total tokens / treatment total tokens`,
 reported alongside the rubric quality score on each side. A workflow that
 uses fewer tokens but produces a worse answer is worse, not better.

## Multi-language benchmark

Three repositories, three languages, 40 hand-labelled questions. This is
the cross-language evidence the bullet "task set of 30–50 questions on at
least three different repositories" above asked for; it does not measure
the end-to-end agent latency / token-cost story (that still needs the
agent harness in items 2-5), but it does answer "does the cascade
generalise beyond the Rust workspace Rust workspace?".

| Repo | Language | Files indexed | Tasks | Recall (cascade default) | Avg s/q | Cheap-path % |
| --- | --- | :-: | :-: | :-: | :-: | :-: |
| `Rust workspace` | Rust | 3 173 | 16 | **16 / 16** | 4.17 s | 19 % |
| `Python codebase` (this user's research repo) | Python | 142 | 12 | **11 / 12** | 2.45 s | 58 % |
| `TypeScript codebase` | TypeScript | 1 903 | 12 | **11 / 12** | 3.83 s | 33 % |
| **Aggregate** | | **5 218** | **40** | **38 / 40 (95 %)** | **3.55 s** | **35 %** |

All four 0.5.0 layer tiers (cascade only, +L2 symbol boost, +L4 PageRank
tiebreaker, full 0.5/0.7 default) hit the same recall on each repo, with
< 0.4 s/q latency variation between tiers. As on Rust workspace, the symbol and
graph layers don't add measurable headroom on these benchmarks because
the cascade alone already gets close to the question-set ceiling — see
the 0.5.1 release notes for why we believe these layers may still help
on harder benchmarks.

The two misses are honest retrieval failures, not label problems:

 - **Python codebase task 4** ("How does the V6 medical-grade <domain-y> benchmark
 resolve and chain its acquire / extract / audit / score steps?")
 Expected `Python codebase/<domain-y>_v6.py` (the orchestration
 layer with `V6Resolution`, `resolve_v6`, `run_step`,
 `command_<domain-y>_v6`). The cascade returned the four
 implementation scripts under
 `examples/scientific_use_cases/genetic_<domain-y>_real_medical_benchmark/v6_medical_grade/scripts/`
 — files that contain `acquire_*`, `extract_*`, `audit_*`,
 `score_*` literally. The cascade preferred surface-token matches
 over the orchestration file's deeper semantic. A user could argue
 the scripts are also a valid answer.

 - **TypeScript codebase task 7** ("How does the assistant decide
 when to automatically compact the conversation history before the
 context window fills?")
 Expected `src/services/compact/autoCompact.ts` (where
 `getAutoCompactThreshold`, `shouldAutoCompact`, `autoCompactIfNeeded`
 live). The cascade returned five other files in
 `src/services/compact/` (`timeBasedMCConfig.ts`, `grouping.ts`,
 `postCompactCleanup.ts`, `prompt.ts`) plus `commands/context/
 context.tsx` — neighbouring concerns but not the actual decision
 logic. The cheap-path early-exited (gap = 0.0467, well above
 τ = 0.015) and didn't reach HyDE escalation; this is the closest
 we've come on the layered system to a query where escalation might
 have helped but the cheap path was over-confident.

Per-repo breakdown of how queries split across cheap vs. escalated path:

 - **Rust workspace** has 13/16 escalated queries because Rust workspace's natural-
 language questions rarely have surface-token overlap with the
 canonical Rust file paths (a query like "how does shell command
 autocompletion generate suggestions while the user is typing"
 needs HyDE to reach `crates/<repo-D>_completer/`).
 - **Python codebase** has 7/12 cheap-path early-exits because Python codebase's filenames
 are very semantic (`<domain-y>_v6.py`, `finite_field_runner.py`,
 `<component-v>.py`) so file-mean cosine alone is
 confident.
 - **TypeScript codebase** sits between (4/12 cheap) — TS
 project conventions mix descriptive filenames with shorter ones
 (`client.ts`, `parser.ts`).

Reproducible runner: ``benchmarks/v0_7_multilang_bench.py``. JSON task
files: ``benchmarks/cross_repo/{Rust workspace,python-codebase,typescript-codebase}.json``. Reproducing the
numbers requires that the three repos be cloned and the per-project
indexes built (``skygrep index .`` from each repo). Index construction
times measured here: Rust workspace 26 K chunks ~25 min, Repo-C 36 K chunks
~17 min, Python codebase 4 K chunks ~3 min, all on Mac CPU with
``OLLAMA_EMBED_MODEL=nomic-embed-text``.

## End-to-end agent benchmark

The four bullets above (items 2-5: real agent harness, controlled model
+ prompt + repo, real agent runs, baseline-vs-treatment headline) have
been answered across three rounds:

 - 6 single-turn easy questions × 2 conditions = 12
 sub-agents.
 - 8 single-turn hard semantic / vocab-mismatch
 questions × 2 conditions = 16 sub-agents.
 - (B) one 3-turn Rust workspace multi-turn session × 2
 conditions = 6 sub-agents (sequential, clean wall-time);
 (C) 6 unused medium-difficulty single-turn questions × 2
 conditions = 12 sub-agents.

Each agent reported a single canonical-file JSON answer plus a
`TOOLS:` audit line of every shell call it made. Token / tool-call /
wall-time totals from each sub-agent's own `usage` telemetry.

### Headline

| Bench | Tasks | rg-only tools | skygrep tools | Δ tools | Δ tokens |
|---|:-:|:-:|:-:|:-:|:-:|
| **Multi-turn (3-turn Rust workspace session)** | 1 × 3 | 38 | **7** | **−82 %** | −5 % |
| 6 medium tasks (-C) | 6 | 25 | **6** | **−76 %** | −8 % |
| 14 single-turn | 14 | 124 | 87 | −30 % | +12 % |
| **20-task single-turn aggregate** | 20 | **149** | **93** | **−37.6 %** | +6.5 % |
| **Strict-label correct (20 tasks)** | — | 12 / 20 | **14 / 20** | **+2** | — |
| Lenient-label correct (20 tasks) | — | 14 / 20 | **17 / 20** | **+3** | — |

### What the data actually shows

 - **Tool-call reduction is the cleanest, most consistent signal.**
 Across single-turn (−37.6 %) and multi-turn (−82 %) the
 direction is the same; multi-turn amplifies the gap because
 rg's wandering compounds across turns while skygrep stays decisive.
 - **Tokens are noisy.** skygrep agents are roughly flat on tokens
 (+6.5 % aggregate), with subset-level swings −8 % to +18 %
 depending on whether the skygrep agent was decisive (1 tool call)
 or wandered through multiple `skygrep` + `Read` rounds. **Don't
 cite skygrep as a token saver.**
 - **Quality slightly better with skygrep.** +2 strict / +3 lenient
 on the 20-task aggregate. skygrep solves the Rust workspace `<domain-y>_v6.py`
 famous miss and typescript-codebase `client.ts` task that rg-only got wrong;
 doesn't lose any task rg-only got right.
 - **Wall-time data is contaminated** by parallel-spawn Ollama
 contention in batches. The multi-turn
 session ran sequentially and is reportable: rg 179 s vs skygrep
 158 s (−12 %).

### Why this matters even when token cost is roughly flat

Each tool call in a Claude Code agent loop costs an LLM round-trip +
network RTT + serialization + context-window growth. A 30-80 %
tool-call reduction means the agent loop is **shorter, faster, and
cleaner** even when total tokens are equal. Different efficiency
dimension from the LLM bill.

Per-task data, full caveats, and the explicit relationship to the
simulated-grep-agent benchmark (#2) are in
[`benchmarks/agent_e2e_results.md`](../benchmarks/agent_e2e_results.md)
and the per-version release notes (`docs/skylakegrep-0.{8,9,10}.0.md`).

## Strongest claims from this repository

 - **Multi-turn agent tool-call reduction**: −82 % fewer tool
 calls in a 3-turn Rust workspace Claude Code session (
 e2e benchmark).
 - **Single-turn agent tool-call reduction**: −37.6 % across 20
 hand-labelled questions in Rust + Python + TypeScript (
 + + single-turn aggregate).
 - **Cross-language retrieval recall**: 38 / 40 (95 %) at 3.55 s/q
 average on Mac CPU across Rust, Python, TypeScript (multi-
 language benchmark, ).
 - **Static-retrieval token reduction vs ripgrep**: ~17.7× on the
 30-task self-test (simulated grep-agent benchmark #2 above) at
 equal recall.
