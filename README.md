# local-mgrep

[![PyPI](https://img.shields.io/pypi/v/local-mgrep?color=0f172a&label=pypi)](https://pypi.org/project/local-mgrep/)
[![Python](https://img.shields.io/badge/python-3.9%2B-0f172a)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-MIT-0f172a)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-published-0f766e)](https://danielchen26.github.io/local-mgrep/)

A command-line semantic code search tool. `local-mgrep` builds a SQLite vector
index from a repository's source files and answers natural-language queries
with line-cited snippets. Indexing, retrieval, and optional answer generation
run on the local host using a local Ollama server; no remote service is
required for the core workflow.

```bash
pip install local-mgrep
ollama pull mxbai-embed-large

mgrep index /path/to/repo --reset
mgrep search "where is token refresh implemented?" -m 10 --json
```

Documentation: <https://danielchen26.github.io/local-mgrep/>.

---

## Contents

- [What it does](#what-it-does)
- [Installation](#installation)
- [Usage](#usage)
- [Architecture](#architecture)
- [CLI reference](#cli-reference)
- [Configuration](#configuration)
- [Capability matrix](#capability-matrix)
- [Benchmark](#benchmark)
- [Development](#development)
- [License](#license)

## What it does

A query is embedded with a local Ollama embedding model and compared by cosine
similarity against the embedded chunks of the repository. A lightweight
lexical token-and-phrase reranker is blended in by default, identical chunk
spans are deduplicated, and the per-file repetition is capped before the final
top-k is returned.

Each result carries a file path, an inclusive 1-based line range, the
detected language, the score, and the verbatim source text. The same
structure is rendered as text, JSON (`--json`), or as a synthesized answer
from a local generation model (`--answer`).

## Installation

### From PyPI

```bash
pip install local-mgrep
```

### From source

```bash
git clone https://github.com/danielchen26/local-mgrep.git
cd local-mgrep
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### Ollama runtime

Install Ollama from <https://ollama.com> and pull at least one embedding
model:

```bash
ollama pull mxbai-embed-large       # default; 1024-dim
# or
ollama pull nomic-embed-text        # smaller alternative
```

A generation model is required only for `--answer` and `--agentic`:

```bash
ollama pull qwen2.5:3b
```

## Usage

### Search by intent

```bash
mgrep search "how does the authentication token get refreshed?"
```

The query is embedded with the same model used at index time, scored against
all chunks, lexically reranked, span-deduplicated, and diversified across
files before output.

### JSON for scripts and agents

```bash
mgrep search "where is the SQLite schema initialized?" -m 10 --json
```

```json
[
  {
    "path":       "local_mgrep/src/storage.py",
    "start_line": 31,
    "end_line":   47,
    "language":   "python",
    "score":      0.824,
    "snippet":    "CREATE TABLE IF NOT EXISTS chunks ..."
  }
]
```

### Filters

```bash
mgrep search "auth token" -m 10 \
  --language python \
  --include "src/*" \
  --exclude "*_test.py"
```

### Synthesized answer

```bash
ollama pull qwen2.5:3b
OLLAMA_LLM_MODEL=qwen2.5:3b mgrep search \
  "how does indexing remove deleted files?" \
  --answer
```

`--answer` passes the retrieved snippets to a local generation model along
with a fixed instruction to cite paths and line ranges; the original sources
are still printed below the synthesized answer.

### Agentic decomposition

```bash
mgrep search "how are tokens created, validated, and refreshed?" \
  --agentic --max-subqueries 3 --answer
```

`--agentic` precedes the search with a generation step that decomposes the
query into related subqueries (default cap 3); each subquery is searched
independently and the union is merged by score before rendering.

### Watch mode

```bash
mgrep watch /path/to/repo --interval 5
```

Polls every interval. New files are indexed, modified files are reindexed,
and missing files are removed from the index.

## Architecture

![local-mgrep system architecture](docs/assets/architecture.svg)

The index pipeline (`mgrep index`, `mgrep watch`) and the query pipeline
(`mgrep search`) communicate only through a SQLite database stored at
`$MGREP_DB_PATH`. The two pipelines share no in-process state and can run
on different hosts as long as they point at the same database file.

The full set of internal modules is listed in
[`docs/local-mgrep-0.2.0.md`](docs/local-mgrep-0.2.0.md#component-overview).

## CLI reference

### `mgrep index`

```bash
mgrep index [PATH] [--reset] [--incremental/--full]
```

Walks `PATH` (default `.`), chunks supported source files, embeds them,
and writes to the index. With `--incremental` (default), only files whose
mtime is newer than the stored value are reindexed and rows for files
that no longer exist under `PATH` are deleted. `--full` reindexes every
discovered file. `--reset` deletes the existing database before indexing.

### `mgrep search`

```bash
mgrep search QUERY [OPTIONS]
```

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
| `--agentic` | off | Decompose the query into subqueries via Ollama before search. |
| `--max-subqueries` | 3 | Upper bound on agentic subqueries. |

### `mgrep stats`

```bash
mgrep stats
```

Prints total chunk count and total indexed file count.

### `mgrep watch`

```bash
mgrep watch [PATH] --interval N
```

Polls every `N` seconds (default 5) until interrupted.

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `OLLAMA_URL` | `http://localhost:11434` | Base URL of the Ollama server. |
| `OLLAMA_EMBED_MODEL` | `mxbai-embed-large` | Embedding model used at index time and query time. |
| `OLLAMA_LLM_MODEL` | `qwen2.5:3b` | Generation model used by `--answer` and `--agentic`. |
| `MGREP_DB_PATH` | `~/.local-mgrep/index.db` | SQLite index location. |

Switching `OLLAMA_EMBED_MODEL` after indexing produces a dimension or
semantic mismatch. Reindex with `--reset` after changing the embedding model.

## Capability matrix

| Capability | Status | Notes |
| --- | --- | --- |
| Semantic code search | implemented | Local Ollama embeddings. |
| Tree-sitter chunking | implemented | Languages with installed grammars; line-window fallback otherwise. |
| `.gitignore` / `.mgrepignore` hygiene | implemented | Plus a built-in skip-set for common build/cache directories. |
| Incremental indexing | implemented | mtime-based; reindexes new and changed files. |
| Stale row cleanup | implemented | Removes rows for files no longer present beneath the indexed root. |
| Watch mode | implemented | Polling loop; default interval 5 seconds. |
| Hybrid lexical + semantic ranking | implemented | Disable with `--semantic-only` for pure cosine. |
| Result diversification | implemented | Per-file cap of 2 chunks before final top-k. |
| Stable JSON output | implemented | See [JSON schema](https://danielchen26.github.io/local-mgrep/#json-schema). |
| Local answer mode | implemented | Uses local Ollama generation model. |
| Local agentic decomposition | implemented | Bounded subquery expansion via Ollama. |
| Hosted account / cloud index | not implemented | Out of scope for this project. |
| Paid web search | not implemented | Out of scope for this project. |

## Benchmark

![local-mgrep deterministic context-gathering benchmark](docs/assets/benchmark.svg)

The repository ships a deterministic benchmark that compares context-gathering
between a grep-agent simulation and a single `mgrep search` per task over 30
repository navigation questions. Token volumes are estimated as `chars / 4`.

| top-k | recall (mgrep) | recall (grep) | estimated total-token reduction | context-token reduction |
| --- | --- | --- | --- | --- |
| 5 | 28 / 30 | 30 / 30 | 2.66× | 5.53× |
| 10 | 30 / 30 | 30 / 30 | 2.00× | 2.90× |
| 20 | 30 / 30 | 30 / 30 | 1.36× | 1.53× |
| 50 | 30 / 30 | 30 / 30 | 0.67× | 0.60× |

```bash
.venv/bin/python benchmarks/token_savings.py --top-k 5
.venv/bin/python benchmarks/agent_context_benchmark.py --top-k 10 --summary-only
```

This is a deterministic local benchmark. It is not provider billing data and
not an answer-quality evaluation. The methodology, the full conditions, and
explicit limitations are documented in
[`docs/token-benchmarking.md`](docs/token-benchmarking.md).

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

.venv/bin/python -m unittest discover tests
.venv/bin/python -m py_compile local_mgrep/src/*.py tests/*.py benchmarks/*.py
```

## License

MIT — see [`LICENSE`](LICENSE).

## Acknowledgments

- [Ollama](https://ollama.com/) for the local embedding and generation runtime.
- [tree-sitter](https://tree-sitter.github.io/tree-sitter/) for syntax-aware parsing.
- Click, NumPy, and SQLite for the core runtime dependencies.
