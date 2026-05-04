<p align="center">
  <img alt="skylakegrep — semantic code search over a local index" src="docs/assets/hero-dark.svg" width="100%">
</p>

<p align="center">
  <a href="https://pypi.org/project/skylakegrep/"><img src="https://img.shields.io/pypi/v/skylakegrep?label=pypi&color=22d3ee&labelColor=0a0d12" alt="PyPI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.9%2B-22d3ee?labelColor=0a0d12" alt="Python 3.9+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-PolyForm--NC--1.0.0-f59e0b?labelColor=0a0d12" alt="PolyForm Noncommercial 1.0.0"></a>
  <a href="https://danielchen26.github.io/skylakegrep/"><img src="https://img.shields.io/badge/docs-published-22d3ee?labelColor=0a0d12" alt="Documentation"></a>
  <a href="https://github.com/danielchen26/skylakegrep/releases/latest"><img src="https://img.shields.io/github/v/release/danielchen26/skylakegrep?label=release&color=22d3ee&labelColor=0a0d12" alt="Latest release"></a>
</p>

<p align="center">
  <a href="#quickstart"><b>Quickstart</b></a>
  &nbsp;·&nbsp;
  <a href="#in-30-seconds"><b>30s demo</b></a>
  &nbsp;·&nbsp;
  <a href="#performance"><b>Performance</b></a>
  &nbsp;·&nbsp;
  <a href="#how-it-works"><b>How it works</b></a>
  &nbsp;·&nbsp;
  <a href="https://github.com/danielchen26/skylakegrep/releases"><b>Releases</b></a>
  &nbsp;·&nbsp;
  <a href="https://danielchen26.github.io/skylakegrep/"><b>Docs</b></a>
</p>

---

`skygrep` is a fully-offline semantic code-search CLI for natural-language
questions about your codebase. **Ask in plain English, get the right file
and line range.** Indexing, retrieval, and optional answer synthesis all
run locally against your own Ollama server. No remote service, no
subscription, no data leaves your machine.

## In 30 seconds

```console
$ pip install skylakegrep
$ ollama pull nomic-embed-text qwen2.5:1.5b qwen2.5:3b   # one-time
$ cd ~/your-project

$ skygrep "where is the cascade tau threshold defined?"
=== skylakegrep/src/storage.py:578-602 (score: 0.781) ===
CASCADE_DEFAULT_TAU = 0.015

def cascade_search(...
[0.51s · cascade=cheap (gap=0.020 τ=0.015) · index 20s ago · 36 files · L2 symbols on · graph prior on]
```

That is the entire happy path. **First query in a fresh project completes
in under 1 s** via a ripgrep fallback while a background process builds
the semantic index. Every query after that uses the full cascade with a
local LLM kept warm in memory.

## Quickstart

```bash
pip install skylakegrep
ollama pull nomic-embed-text qwen2.5:1.5b qwen2.5:3b   # ~3 GB total

# One-time: register skylakegrep with detected LLM CLIs
# (Claude Code / Codex / OpenCode / Gemini CLI / Cursor)
skygrep setup

cd /your/project
skygrep "<your question>"
skygrep doctor                # verify runtime + models + index + integrations
skygrep stats                 # show current project's index info
```

`skygrep` derives the project root from `git rev-parse --show-toplevel`
(falling back to the working directory) and keeps a per-project index
under `~/.skylakegrep/repos/`. Subcommand names (`index`, `doctor`,
`stats`, `watch`, `serve`, `setup`, `enrich`) take precedence — anything
else is treated as a query, so `skygrep "stats and metrics"` (quoted) is
unambiguous.

**`skygrep setup`** writes a small markdown snippet to each detected LLM
CLI's user-level instructions file (e.g. `~/.claude/CLAUDE.md`,
`~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md`) telling the agent to
prefer `skygrep` for natural-language code search and fall back to `rg`
otherwise. Snippets are delimited by markers; `skygrep setup --uninstall`
removes them cleanly without touching your other instructions.

## Performance

Measured on a Mac M-series CPU, no GPU. Three repositories, three
languages, 40 hand-labelled questions:

| Repo | Language | Tasks | Recall | Avg s/q |
| --- | --- | :-: | :-: | :-: |
| `Rust workspace` | Rust | 16 | **16 / 16** | 4.17 |
| `Python codebase` | Python | 12 | **11 / 12** | 2.45 |
| `TypeScript codebase` | TypeScript | 12 | **11 / 12** | 3.83 |
| **Aggregate** | | **40** | **38 / 40 (95 %)** | **3.55** |

Reproducible runner at `benchmarks/v0_7_multilang_bench.py`. Per-repo
breakdown, per-tier comparison (cascade only / +L2 / +L4 / full
default), and the two honest misses are documented in
[`docs/parity-benchmarks.md`](docs/parity-benchmarks.md).

Recall counts a query as a hit when at least one of the top-10
returned chunks matches the canonical answer dirs (or any listed
`expected_alternatives`).

### Tier breakdown (16-task Rust)

| Tier | What it does | Recall | Cold first query | Warm avg s/q |
| --- | --- | :-: | :-: | :-: |
| **cascade** ⭐ default | rg prefilter → file-mean cosine → escalate to HyDE only when uncertain | **16 / 16** | ~10 s (Ollama loads) | **4.2 s** |
| cascade-cheap | early-exit only, no LLM call | 11 / 16 | <1 s | <0.2 s |
| cascade + small HyDE (`OLLAMA_HYDE_MODEL=qwen2.5:1.5b`) | uses 1.5 B for HyDE — faster, slightly lower recall | 15 / 16 | ~5 s | **2.0 s** |
| chunk + rerank | classic chunk cosine + cross-encoder rerank | 11 / 16 | ~10 s + 30 s reranker load | ~10 s |
| ripgrep raw | `rg -il -F` token-OR (file membership only) | 16 / 16 | <1 s | <1 s |

The cascade is bimodal by design: ~80 % of queries take the cheap path
(file-mean cosine, no LLM call) and complete in under 200 ms warm; the
remaining ~20 % escalate to a HyDE-augmented retrieval and complete in
the 1–2 s band. With Ollama models kept resident in memory
(`OLLAMA_KEEP_ALIVE=-1`, the 0.6.0 default) the second query in a shell
session no longer pays the 5–10 s Ollama cold-load.

A second [self-test benchmark](docs/token-benchmarking.md) compares
`skygrep` against a simulated grep agent over 30 navigation tasks against
this repo: 30 / 30 recall at top-k 10 with **2× total-token reduction**
and **2.9× context-token reduction** vs the agent baseline.

## How it works

```
your query
    │
    ▼
┌─────────────────────────────────────────────────────────────────┐
│ 1.  ripgrep prefilter      Fast surface-token narrowing          │
│ 2.  file-mean cosine       Rank files by mean of chunk vectors   │
│ 3.  cascade decision       Confident? return cheap. Else escalate│
│ 4.  HyDE escalation        LLM rewrites query → cosine union     │
│ 5.  symbol + graph         Tree-sitter symbol boost + PageRank   │
│     (L2 + L4)              tiebreaker on near-tied candidates    │
└─────────────────────────────────────────────────────────────────┘
    │
    ▼
top-K chunks (path · line range · score · snippet)
```

Each layer is **offline-paid and query-time-free where possible**:
embeddings are precomputed at index time, symbol extraction runs once
per project, the file-export PageRank is one regex pass over the corpus.
Only the cascade's HyDE-escalation path makes a query-time LLM call, and
it only runs on the ~20 % of queries the cheap path is uncertain about.

The full architecture diagram and module-by-module walk-through is at
[`docs/skylakegrep-0.1.0.md`](docs/skylakegrep-0.1.0.md) and
[`docs/roadmap.md`](docs/roadmap.md).

## When to use what

| You want | Use |
| --- | --- |
| Find code by concept ("how does X work?") | `skygrep "<query>"` |
| Find code with a known token | `rg <token>` (it's faster, no setup) |
| Synthesize an answer with citations | `skygrep "<query>" --answer` |
| Decompose a broad question | `skygrep "<query>" --agentic --max-subqueries 3 --answer` |
| Machine-readable output for an agent | `skygrep "<query>" --json` |
| Re-rank candidates with a cross-encoder | `skygrep "<query>" --no-cascade --rerank` |
| Continuously index a watched dir | `skygrep watch /path` |
| Keep the cross-encoder warm across queries | `skygrep serve & ; skygrep "<q>" --daemon-url http://127.0.0.1:7878` |

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama server URL. |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Embedding model. Switching requires `skygrep index --reset`. |
| `OLLAMA_LLM_MODEL` | `qwen2.5:3b` | Used for `--answer` and `--agentic`. |
| `OLLAMA_HYDE_MODEL` | `qwen2.5:3b` | Used for cascade-escalation HyDE. Falls back to `OLLAMA_LLM_MODEL` if not installed. Set to `qwen2.5:1.5b` for ~30 % speedup at the cost of 1 task on 16-task Rust. |
| `OLLAMA_KEEP_ALIVE` | `-1` | Passed to every Ollama call. `-1` keeps models resident indefinitely (recommended). |
| `SKYGREP_DB_PATH` | per-project | When set, skygrep treats the index as curated and disables auto-mutation. |
| `SKYGREP_AUTO_PULL` | unset | Set `yes` to auto-`ollama pull` missing models without prompting. |
| `SKYGREP_AUTO_REFRESH_THROTTLE_SECONDS` | `30` | Skip the mtime scan if the previous refresh ran more recently. |
| `SKYGREP_RERANK_MODEL` | `mixedbread-ai/mxbai-rerank-large-v2` | Cross-encoder for `--rerank`. |
| `SKYGREP_RERANK_POOL` | `50` | Candidate pool before reranking. |

## Releases

This is the first public release of `skylakegrep`. See
[`docs/skylakegrep-0.1.0.md`](docs/skylakegrep-0.1.0.md) for the full
description of capabilities, architecture, CLI flags, and
environment variables.

  - **0.1.0** — first public release. LLM-driven query routing
    (filename / lexical / semantic cascade tiers, intent-aware
    merge), confidence-gated semantic cascade with HyDE escalation +
    cross-encoder rerank + PageRank tiebreaker, lazy PDF / docx
    content extraction (`pdftotext` + `pypdf` fallback, optional
    `--ocr`), framed Pygments-highlighted card rendering, four
    `--detail` levels, `skygrep setup` auto-registration with major
    LLM CLIs (Claude Code, Codex, OpenCode, Gemini CLI, Cursor).
    PolyForm Noncommercial 1.0.0 license.

## CLI reference

```
skygrep setup    [--list|--uninstall|--yes] # register with Claude Code / Codex / OpenCode / Gemini / Cursor
skygrep "<query>" [OPTIONS]                 # bare-form search
skygrep search   "<query>" [OPTIONS]        # explicit search
skygrep doctor                              # health check
skygrep stats                               # project index info
skygrep index    [PATH] [--reset]           # explicit reindex
skygrep watch    [PATH] --interval N        # poll for changes
skygrep serve    [--host H] [--port P]      # warm-reranker daemon
skygrep enrich   [--max N] [--batch B]      # opt-in doc2query enrichment
```

<details>
<summary><b><code>skygrep search</code> options</b></summary>

<br>

| Option | Default | Effect |
| --- | --- | --- |
| `-m`, `-n`, `--top` | 5 | Number of final results. |
| `--json` | off | Emit a JSON array; suppresses human formatting. |
| `--answer` | off | Synthesize an answer from retrieved snippets via Ollama. |
| `--content / --no-content` | on | Show or hide snippet bodies in human output. |
| `--language` | — | Restrict to one or more language keys; repeatable. |
| `--include` / `--exclude` | — | Glob filter (repeatable). |
| `--cascade / --no-cascade` | on | Confidence-gated retrieval. Off = chunk-only legacy path. |
| `--cascade-tau` | 0.015 | Top-1 / top-2 file-mean cosine gap above which to early-exit. |
| `--rerank / --no-rerank` | on | Cross-encoder rerank on the non-cascade path. |
| `--rerank-pool` | 50 | Candidate pool before reranking. |
| `--rerank-model` | env or default | HuggingFace cross-encoder id. |
| `--hyde / --no-hyde` | off | Force HyDE outside the cascade (rare; cascade decides per query). |
| `--multi-resolution / --no-multi-resolution` | on | File-level cosine top-N → chunk-level inside those files. |
| `--file-top` | 30 | Files surfaced by file-level retrieval. |
| `--lexical-prefilter / --no-lexical-prefilter` | on | Use ripgrep to narrow the candidate file set. |
| `--lexical-root` | cwd / git toplevel | Root directory ripgrep scans. |
| `--lexical-min-candidates` | 2 | Fall back to corpus-wide cosine when ripgrep returns fewer files. |
| `--rank-by` | `chunk` | `chunk` (per-file diversity cap) or `file` (one chunk per file). |
| `--auto-index / --no-auto-index` | on | Auto-build the index on first query and refresh on mtime change. |
| `--daemon-url` | — | Send the search to a running `skygrep serve` daemon. |
| `--agentic` | off | Decompose into subqueries via Ollama before search. |
| `--max-subqueries` | 3 | Upper bound on agentic subqueries. |
| `--semantic-only` | off | Skip lexical reranking; rank by cosine alone. |

</details>

<details>
<summary><b>Capability matrix (every feature, when introduced)</b></summary>

<br>

| Capability | Since |
| --- | --- |
| Semantic code search via local Ollama | 0.1.0 |
| Tree-sitter chunking + line-window fallback | 0.2.0 |
| `.gitignore` / `.skygrepignore` hygiene | 0.2.0 |
| Incremental indexing (mtime-based) | 0.2.0 |
| Stale row cleanup | 0.2.0 |
| Watch mode | 0.2.0 |
| Hybrid lexical + semantic ranking | 0.2.0 |
| Stable JSON output | 0.2.0 |
| Local answer mode | 0.2.0 |
| Local agentic decomposition | 0.2.0 |
| Cross-encoder rerank | 0.3.0 |
| Asymmetric query/document embedding prefixes | 0.3.0 |
| HyDE query rewriting | 0.3.0 |
| Multi-resolution retrieval | 0.3.0 |
| Lexical prefilter (ripgrep first stage) | 0.3.0 |
| File-rank (one chunk per file) | 0.3.0 |
| Daemon mode | 0.3.0 |
| Quantisation / device knobs | 0.3.0 |
| Confidence-gated cascade | 0.3.0 (default in 0.4.0) |
| Bare-form invocation `skygrep "<q>"` | 0.4.0 |
| Per-project auto-index | 0.4.0 |
| `skygrep doctor` health check | 0.4.0 |
| Ripgrep fallback for first query | 0.4.1 |
| Symbol-aware indexing (L2) | 0.5.0 |
| doc2query enrichment (L3, opt-in via `skygrep enrich`) | 0.5.0 |
| File-export PageRank tiebreaker (L4) | 0.5.0 |
| Cascade file-mean cosine corpus-wide | 0.5.1 |
| Smaller default HyDE model + `keep_alive=-1` | 0.6.0 |

</details>

## Development

```bash
git clone https://github.com/danielchen26/skylakegrep.git
cd skylakegrep
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[rerank]"

.venv/bin/pytest -q tests/
.venv/bin/python benchmarks/agent_context_benchmark.py --top-k 10 --summary-only
```

To reproduce a Rust workspace benchmark row, follow the indexing/run instructions
in [`docs/parity-benchmarks.md`](docs/parity-benchmarks.md).

## License

**PolyForm Noncommercial 1.0.0** — see [`LICENSE`](LICENSE).

Personal, academic, research, hobby, and any other non-commercial
use is fully permitted, including modification and redistribution.
**Commercial use is NOT permitted** under this license. To obtain
a commercial license, open a GitHub issue titled "Commercial
license inquiry" or email <chentianchi@gmail.com>.

## Acknowledgments

- [Ollama](https://ollama.com/) for the local embedding and generation runtime.
- [tree-sitter](https://tree-sitter.github.io/tree-sitter/) for syntax-aware parsing.
- [ripgrep](https://github.com/BurntSushi/ripgrep) for the lexical prefilter stage.
- [Mixedbread](https://www.mixedbread.com/) for the open-source `mxbai-rerank-*-v2` cross-encoder family.
- [nomic-embed-text](https://www.nomic.ai/blog/posts/nomic-embed-text-v1) for the embedding model.
- Click, NumPy, and SQLite for the core runtime dependencies.
