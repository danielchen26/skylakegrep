<p align="center">
  <img alt="local-mgrep — semantic code search over a local index" src="docs/assets/hero-dark.svg" width="100%">
</p>

<p align="center">
  <a href="https://pypi.org/project/local-mgrep/"><img src="https://img.shields.io/pypi/v/local-mgrep?label=pypi&color=22d3ee&labelColor=0a0d12" alt="PyPI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.9%2B-22d3ee?labelColor=0a0d12" alt="Python 3.9+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-22d3ee?labelColor=0a0d12" alt="MIT License"></a>
  <a href="https://danielchen26.github.io/local-mgrep/"><img src="https://img.shields.io/badge/docs-published-22d3ee?labelColor=0a0d12" alt="Documentation"></a>
  <a href="https://github.com/danielchen26/local-mgrep/releases/latest"><img src="https://img.shields.io/github/v/release/danielchen26/local-mgrep?label=release&color=22d3ee&labelColor=0a0d12" alt="Latest release"></a>
</p>

<p align="center">
  <a href="https://danielchen26.github.io/local-mgrep/"><b>Documentation</b></a>
  &nbsp;·&nbsp;
  <a href="#quickstart"><b>Quickstart</b></a>
  &nbsp;·&nbsp;
  <a href="#architecture"><b>Architecture</b></a>
  &nbsp;·&nbsp;
  <a href="#benchmark"><b>Benchmark</b></a>
  &nbsp;·&nbsp;
  <a href="https://github.com/danielchen26/local-mgrep/releases"><b>Releases</b></a>
  &nbsp;·&nbsp;
  <a href="https://github.com/danielchen26/local-mgrep/issues"><b>Issues</b></a>
</p>

---

## Overview

`local-mgrep` is an offline semantic code-search CLI built around a four-stage
retrieval pipeline:

1. **Lexical prefilter (ripgrep).** Up to eight literal tokens are extracted
   from the natural-language query and ripgrep narrows the corpus to files
   containing any of them. Typically reduces the chunk scan ~100×.
2. **Multi-resolution cosine.** A file-level embedding (mean of chunk
   vectors, computed at index time) picks the top files; chunk-level cosine
   then runs only inside those files.
3. **Confidence-gated cascade (`--cascade`, opt-in, v0.3.0).** When the
   cheap file-mean retrieval has a clear top-1, we return immediately. Only
   uncertain queries pay the LLM-driven escalation (cosine + file-rank ∪
   HyDE-rewritten cosine + file-rank, score-preserved union).
4. **Optional cross-encoder rerank.** When `--rerank` is on (default for the
   non-cascade path), an `mxbai-rerank-*-v2` cross-encoder reorders the
   top candidate pool; non-canonical paths (tests, blocklists) get a
   multiplicative penalty.

Indexing, retrieval, optional answer synthesis (`--answer`), and optional
agentic decomposition (`--agentic`) all run on the local host against a
local Ollama server. No remote service is required for the core workflow.

Each result carries the file path, an inclusive 1-based line range, the
detected language, the score, and the verbatim source text — rendered as
text, JSON (`--json`), or as a synthesized answer over the local Ollama
generation model.

> **What's new in 0.3.0** — `mgrep search --cascade` (opt-in) hits **14/16
> recall on the <repo-D> 16-task cross-repo benchmark at 1.49 s/query on
> Mac CPU**, the same recall as the previous max-accurate tier
> (`--rerank --hyde --rank-by file`, 21.8 s/q) at **14× lower latency**.
> See [Benchmark](#benchmark) and the [v0.3.0 release notes](docs/local-mgrep-0.3.0.md).

## Quickstart

```bash
# 1. Install
pip install local-mgrep

# 2. Pull a local embedding model (one-time)
ollama pull nomic-embed-text         # recommended — supports query/doc prefixes
# alternative: ollama pull mxbai-embed-large

# 3. Index your repository (the rg prefilter is on by default)
mgrep index /path/to/repo --reset

# 4. Ask in natural language — daily-driver mode
mgrep search "where is the auth token refreshed?" -m 10

# 5. Or use the cascade for max recall at low latency (opt-in)
mgrep search "where is the websocket reconnect logic?" -m 10 --cascade
```

For machine-readable output suitable for scripts and coding agents:

```bash
mgrep search "where is the SQLite schema initialized?" -m 10 --json
```

To synthesize an answer from the retrieved snippets via a local Ollama
generation model (the original ranked sources are still printed below):

```bash
ollama pull qwen2.5:3b
mgrep search "how does indexing remove deleted files?" --answer
```

To decompose a broad question into bounded local subqueries:

```bash
mgrep search "how are tokens created, validated, and refreshed?" \
  --agentic --max-subqueries 3 --answer
```

For interactive multi-query sessions (eliminates ~5–10 s of cross-encoder
cold-load per call):

```bash
mgrep serve &                                          # one terminal
mgrep search "..." --daemon-url http://127.0.0.1:7878  # another
```

Full CLI reference and configuration: <https://danielchen26.github.io/local-mgrep/>.

## Architecture

<p align="center">
  <img alt="local-mgrep system architecture: three lanes for index time, storage, and query time" src="docs/assets/architecture.svg" width="100%">
</p>

The index pipeline (`mgrep index`, `mgrep watch`) and the query pipeline
(`mgrep search`) communicate only through a SQLite database stored at
`$MGREP_DB_PATH`. The two pipelines share no in-process state and can run
on different hosts as long as they point at the same database file. The
index pipeline additionally populates a `files` table at index time
(L2-normalised mean of chunk vectors per file) so the query path can do
file-level retrieval without re-scanning chunks.

Three retrieval tiers are exposed through `mgrep search`:

| Tier | Command | Recall (<repo-D> 16) | Avg s/q (Mac CPU) | Notes |
| --- | --- | :-: | :-: | --- |
| daily-driver | `mgrep search` (defaults) | 9/16 | **0.52** | rg prefilter + cosine + file-rank, no rerank |
| standard | `--rerank` | 11/16 | 9.5 | adds `mxbai-rerank-base-v2` cross-encoder |
| **cascade** ⭐ | `--cascade` | **14/16** | **1.49** | confidence-gated; cheap path on ~81% of queries, HyDE-union escalation on the rest |
| previous max | `--rerank --hyde --rank-by file` | 14/16 | 21.8 | superseded by `--cascade` (same recall, 14× faster) |

The full set of internal modules is documented at
<https://danielchen26.github.io/local-mgrep/#architecture>.

## Capability matrix

| Capability | Status | Since | Notes |
| --- | --- | --- | --- |
| Semantic code search via local Ollama | implemented | 0.1.0 | No remote service required. |
| Tree-sitter chunking | implemented | 0.2.0 | Languages with installed grammars; line-window fallback otherwise. |
| `.gitignore` / `.mgrepignore` hygiene | implemented | 0.2.0 | Plus a built-in skip-set for common build/cache directories. |
| Incremental indexing | implemented | 0.2.0 | mtime-based; reindexes new and changed files. |
| Stale row cleanup | implemented | 0.2.0 | Removes rows for files no longer present beneath the indexed root. |
| Watch mode | implemented | 0.2.0 | Polling loop; default interval 5 seconds. |
| Hybrid lexical + semantic ranking | implemented | 0.2.0 | Disable with `--semantic-only` for pure cosine. |
| Stable JSON output | implemented | 0.2.0 | See [JSON schema](https://danielchen26.github.io/local-mgrep/#json-schema). |
| Local answer mode | implemented | 0.2.0 | Uses local Ollama generation model. |
| Local agentic decomposition | implemented | 0.2.0 | Bounded subquery expansion via Ollama. |
| Cross-encoder rerank | implemented | 0.3.0 | `--rerank` (default on); models: `mxbai-rerank-base-v2` (default), `mxbai-rerank-large-v2` (`MGREP_RERANK_MODEL`). |
| Asymmetric query/document prefixes | implemented | 0.3.0 | Auto-applied for `nomic-embed-text` and similar. |
| HyDE query rewriting | implemented | 0.3.0 | `--hyde`; deterministic seed, single Ollama generation. |
| Multi-resolution retrieval | implemented | 0.3.0 | File-level cosine top-N → chunk-level inside those files (default on). |
| Lexical prefilter (ripgrep first stage) | implemented | 0.3.0 | `--lexical-prefilter` (default on); `--lexical-root`, `--lexical-min-candidates`. |
| File-rank (one chunk per file) | implemented | 0.3.0 | `--rank-by file`; collapses results so small canonical files compete fairly. |
| Daemon mode (warm reranker) | implemented | 0.3.0 | `mgrep serve` + `--daemon-url`; eliminates ~5–10 s cold-load per call. |
| Quantisation / device knobs | implemented | 0.3.0 | `MGREP_RERANK_QUANTIZE=int8`, `MGREP_RERANK_DEVICE=auto/mps/cpu`. |
| **Confidence-gated cascade** | **implemented** | **0.3.0** | `--cascade` + `--cascade-tau`; **14/16 <repo-D> recall @ 1.49 s/q** — see [Benchmark](#benchmark). |
| Hosted account / cloud index | out of scope | — | Not planned. |
| Paid web search | out of scope | — | Not planned. |

## Benchmark

Two reproducible benchmarks ship in this repository.

### 1. <repo-D> 16-task cross-repo benchmark (the headline)

Measured on Mac CPU, no daemon, cold reranker amortised over 16 tasks. The
test set is a held-out collection of natural-language queries against the
`<repo-D>-terminal/<repo-D>` Rust workspace; the canonical answers are subdirectory
paths (e.g. `crates/voice_input/`). Recall counts a query as a hit when at
least one returned chunk lives under the canonical subdirectory.

| Tier | Command | Recall | Avg s/q |
| --- | --- | :-: | :-: |
| ultra-fast | `mgrep search --cascade --cascade-tau 0.0` | 11/16 | **0.10** |
| daily-driver | `mgrep search` (defaults) | 9/16 | 0.52 |
| standard | `mgrep search --rerank` | 11/16 | 9.5 |
| **cascade ⭐ (default τ)** | `mgrep search --cascade` | **14/16** | **1.49** |
| previous max | `mgrep search --rerank --hyde --rank-by file` | 14/16 | 21.8 |
| ripgrep raw recall | `rg -il -F` token-OR | 16/16 | 0.1 |

The cascade is **the new max-accurate tier**: same 14/16 ceiling as the
previous LLM-augmented configuration, at one fourteenth the latency. The
cheap file-mean path handles ~81% of queries at the default τ=0.015; only
the remaining ~19% pay the HyDE-union escalation. Two queries
(`crates/ai/`, `app/src/billing/`) currently miss across every tested
configuration — these are hard semantic-disambiguation cases discussed in
[`docs/roadmap.md`](docs/roadmap.md). Full sweep, methodology, and a τ
ablation table are in [`docs/parity-benchmarks.md`](docs/parity-benchmarks.md).

### 2. local-mgrep self-test (regression guard)

<p align="center">
  <img alt="Bar chart: token reduction and recall vs grep-agent across top-k 5, 10, 20, 50" src="docs/assets/benchmark.svg" width="100%">
</p>

Deterministic local benchmark over 30 repository-navigation tasks. Compares
a single `mgrep search` call against a grep-agent simulation. Token volumes
are estimated as `chars / 4`.

| top-k | recall (mgrep) | recall (grep) | total-token reduction | context-token reduction |
| ----: | :------------- | :------------ | :-------------------- | :---------------------- |
| 5     | 28 / 30        | 30 / 30       | 2.66×                 | 5.53×                   |
| **10** | **30 / 30**    | **30 / 30**   | **2.00×**             | **2.90×**               |
| 20    | 30 / 30        | 30 / 30       | 1.36×                 | 1.53×                   |
| 50    | 30 / 30        | 30 / 30       | 0.67×                 | 0.60×                   |

This is the regression guard — every release is verified to keep 30/30 at
top-k 10. Full methodology and limitations are in
[`docs/token-benchmarking.md`](docs/token-benchmarking.md).

## CLI reference

```bash
mgrep index   [PATH] [--reset] [--incremental/--full]    # build or refresh the index
mgrep search  QUERY  [OPTIONS]                           # retrieve ranked snippets
mgrep serve   [--host H] [--port P]                      # run daemon (keeps reranker warm)
mgrep stats                                              # print chunk and file counts
mgrep watch   [PATH] --interval N                        # poll for changes (default 5s)
```

<details>
<summary><b><code>mgrep search</code> options</b></summary>

<br>

| Option | Default | Effect |
| --- | --- | --- |
| `-m`, `-n`, `--top` | 5 | Number of final results. |
| `--json` | off | Emit a JSON array; suppresses human formatting. |
| `--answer` | off | Synthesize an answer from retrieved snippets via Ollama. |
| `--content / --no-content` | on | Show or hide snippet bodies in human output. |
| `--language` | — | Restrict to one or more language keys; repeatable. |
| `--include` | — | Glob; only paths matching at least one pattern are kept; repeatable. |
| `--exclude` | — | Glob; paths matching any pattern are dropped; repeatable. |
| `--semantic-only` | off | Skip lexical reranking; rank by cosine alone. |
| `--rerank / --no-rerank` | on | Apply cross-encoder rerank when `sentence-transformers` is installed. |
| `--rerank-pool` | 50 | Candidate pool size before reranking (env `MGREP_RERANK_POOL`). |
| `--rerank-model` | env or default | HuggingFace cross-encoder id (env `MGREP_RERANK_MODEL`). |
| `--hyde / --no-hyde` | off | Generate a hypothetical-document via local Ollama LLM and embed both the query and the doc. |
| `--multi-resolution / --no-multi-resolution` | on | Two-stage retrieval: file-level cosine top-N, then chunk-level inside those. |
| `--file-top` | 30 | Number of files surfaced by file-level retrieval before chunk-level scoring. |
| `--lexical-prefilter / --no-lexical-prefilter` | on | Use ripgrep to narrow the candidate file set before cosine + rerank. |
| `--lexical-root` | cwd | Root directory ripgrep scans for the lexical prefilter. |
| `--lexical-min-candidates` | 2 | Fall back to corpus-wide cosine when ripgrep returns fewer files. |
| `--rank-by` | `chunk` | `chunk` (per-file diversity cap) or `file` (one best chunk per file, sorted by score). |
| `--cascade / --no-cascade` | off | **0.3.0**: confidence-gated retrieval (cheap path + HyDE-union escalation). |
| `--cascade-tau` | 0.015 | **0.3.0**: confidence threshold (top-1 minus top-2 file-mean cosine). |
| `--daemon-url` | — | Send the search to a running `mgrep serve` daemon (warm reranker). |
| `--agentic` | off | Decompose the query into subqueries via Ollama before search. |
| `--max-subqueries` | 3 | Upper bound on agentic subqueries. |

</details>

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `OLLAMA_URL` | `http://localhost:11434` | Base URL of the Ollama server. |
| `OLLAMA_EMBED_MODEL` | `mxbai-embed-large` | Embedding model used at index time and query time. `nomic-embed-text` is the recommended alternative for code. |
| `OLLAMA_LLM_MODEL` | `qwen2.5:3b` | Generation model used by `--answer`, `--agentic`, `--hyde`, and the cascade escalation. |
| `MGREP_DB_PATH` | `~/.local-mgrep/index.db` | SQLite index location. |
| `MGREP_RERANK_MODEL` | `mixedbread-ai/mxbai-rerank-base-v2` | Cross-encoder model id. Try `mxbai-rerank-large-v2` for +1 recall at ~2× latency. |
| `MGREP_RERANK_POOL` | 50 | Candidate pool size before reranking. |
| `MGREP_RERANK_QUANTIZE` | — | Set `int8` for x86_64 CPUs with VNNI; no benefit on Apple Silicon. |
| `MGREP_RERANK_DEVICE` | `auto` | `auto`, `mps`, or `cpu`. |

> **Note.** Switching `OLLAMA_EMBED_MODEL` after indexing produces a
> dimension or semantic mismatch. Reindex with `--reset` after changing the
> embedding model.

## Releases

Every released version has a comprehensive entry on the
[GitHub Releases page](https://github.com/danielchen26/local-mgrep/releases)
covering the architecture change, benchmark deltas, null-result findings,
and compatibility notes. The latest release is also published to PyPI.

| Version | Release notes | PyPI |
| --- | --- | --- |
| **0.3.0** ⭐ | [v0.3.0 — confidence-gated cascade](https://github.com/danielchen26/local-mgrep/releases/tag/v0.3.0) ([docs](docs/local-mgrep-0.3.0.md)) | <https://pypi.org/project/local-mgrep/0.3.0/> |
| 0.2.0 | [v0.2.0 — vectorized retrieval, lexical reranker](https://github.com/danielchen26/local-mgrep/releases/tag/v0.2.0) ([docs](docs/local-mgrep-0.2.0.md)) | <https://pypi.org/project/local-mgrep/0.2.0/> |
| 0.1.0 | [v0.1.0](https://github.com/danielchen26/local-mgrep/releases/tag/v0.1.0) | <https://pypi.org/project/local-mgrep/0.1.0/> |

## Development

```bash
git clone https://github.com/danielchen26/local-mgrep.git
cd local-mgrep
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[rerank]"

.venv/bin/pytest -q tests/
.venv/bin/python benchmarks/agent_context_benchmark.py --top-k 10 --summary-only
```

To reproduce a <repo-D> benchmark row, follow the indexing/run instructions
in [`docs/parity-benchmarks.md`](docs/parity-benchmarks.md).

## License

MIT — see [`LICENSE`](LICENSE).

## Acknowledgments

- [Ollama](https://ollama.com/) for the local embedding and generation runtime.
- [tree-sitter](https://tree-sitter.github.io/tree-sitter/) for syntax-aware parsing.
- [ripgrep](https://github.com/BurntSushi/ripgrep) for the lexical prefilter stage.
- [Mixedbread](https://www.mixedbread.com/) for the open-source `mxbai-rerank-*-v2`
  cross-encoder family used by `--rerank`.
- Click, NumPy, and SQLite for the core runtime dependencies.
