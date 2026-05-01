# local-mgrep 0.2.0 Capability Guide

`local-mgrep` is a free, local-first semantic code search CLI. It is designed to
capture the highest-value workflow ideas from the original Mixedbread `mgrep`
without requiring a hosted account, paid API, cloud index, or remote code upload.

## Local-first scope

The project intentionally keeps all core work on the user's machine:

- **Embeddings:** generated through a local Ollama embedding model.
- **Search index:** stored in a local SQLite database.
- **Answer synthesis:** optional local Ollama text-generation model.
- **Agentic search:** optional local Ollama query decomposition.
- **No cloud dependency:** no login, organization switch, hosted store, or paid web
  search is required.

This means `local-mgrep` is not a one-to-one clone of the original hosted product.
It aims to be a strong local replacement for semantic repo search and local agent
workflows.

## Installed commands

```bash
mgrep index [PATH] [--reset] [--incremental/--full]
mgrep search QUERY [OPTIONS]
mgrep stats
mgrep watch [PATH] [--interval N]
```

## Indexing behavior

`mgrep index` scans supported source files, chunks them, embeds the chunks through
Ollama, and writes vectors plus source metadata to SQLite.

Supported source extensions include Python, JavaScript, TypeScript, TSX/JSX, Go,
Rust, Java, C/C++, C#, Ruby, PHP, Swift, Kotlin, Scala, Vue, and Svelte.

Index hygiene is a first-class feature:

- `.gitignore` is respected.
- `.mgrepignore` is respected.
- Common generated/vendor/cache directories are skipped by default, including
  `.git`, `.venv`, `node_modules`, `dist`, `build`, `target`, `vendor`, and
  `__pycache__`.
- Incremental indexing removes stale rows for deleted files under the indexed
  root, so old results do not continue to appear after files are removed.
- `watch` mode adds, updates, and deletes index entries as the project changes.

## Search behavior

Search is semantic: users describe intent rather than exact code text. Results are
ranked by local embedding similarity, deduplicated by logical source span, and
rendered with provenance.

Human output includes file path, line range, score, and snippet by default:

```bash
mgrep search "where is token validation implemented"
```

JSON output is stable for scripts and agents:

```bash
mgrep search "where is token validation implemented" --json
```

Each JSON result contains:

```json
{
  "path": "src/auth.py",
  "start_line": 10,
  "end_line": 14,
  "language": "python",
  "score": 0.82,
  "snippet": "def validate_token(...): ..."
}
```

## Local parity flags

The 0.2.0 CLI adds the highest-value original-style flags that make sense in a
local tool:

```bash
mgrep search "auth token" -m 10
mgrep search "auth token" --content
mgrep search "auth token" --no-content
mgrep search "auth token" --language python
mgrep search "auth token" --include "src/*"
mgrep search "auth token" --exclude "*_test.py"
```

These flags are applied before final ranking output where practical, reducing
noise and making the tool more scriptable.

## Local answer mode

`--answer` uses a local Ollama generation model to synthesize an answer from the
retrieved local snippets only:

```bash
OLLAMA_LLM_MODEL=qwen2.5:3b mgrep search "how is token validation implemented" --answer
```

The answer includes source citations so users can inspect the exact local file and
line range. No external API is used.

## Local agentic mode

`--agentic` uses the local answer model to split a broad question into bounded
subqueries, searches each subquery locally, then merges and deduplicates the
results:

```bash
mgrep search "how are access tokens created, validated, and refreshed" --agentic --json -m 10
mgrep search "how are access tokens created, validated, and refreshed" --agentic --answer
```

The default cap is 3 generated subqueries, configurable with:

```bash
mgrep search "token lifecycle" --agentic --max-subqueries 5
```

The bound prevents local LLM decomposition from exploding runtime on large repos.

## Performance improvements in 0.2.0

The current implementation improves both indexing and query-time behavior:

- Indexing uses `embed_batch` when the embedder provides it.
- Ollama embedding attempts the local `/api/embed` batch endpoint before falling
  back to single-text `/api/embeddings` calls.
- Search loads chunk metadata and vectors in one joined query.
- Vector scoring uses NumPy matrix operations instead of a pure Python loop.
- Language/path filters reduce candidate rows before ranking output.

These changes move `local-mgrep` much closer to a daily-driver local search tool,
especially for repeated searches over an existing index.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `OLLAMA_URL` | `http://localhost:11434` | Local Ollama server URL |
| `OLLAMA_EMBED_MODEL` | `mxbai-embed-large` | Local embedding model |
| `OLLAMA_LLM_MODEL` | `qwen2.5:3b` | Local generation model for `--answer` and `--agentic` |
| `MGREP_DB_PATH` | `~/.local-mgrep/index.db` | SQLite index location |

Recommended fully local setup:

```bash
ollama pull nomic-embed-text
ollama pull qwen2.5:3b
pip install local-mgrep
OLLAMA_EMBED_MODEL=nomic-embed-text mgrep index /path/to/repo --reset
```

## Comparison with original mgrep

Implemented locally:

- Semantic repo search
- Watch mode
- Result count flag
- Content/no-content output
- JSON output
- Ignore files
- Answer synthesis
- Agentic query decomposition
- Local-only indexing and storage

Intentionally not implemented because they depend on hosted/product features:

- Login/logout and organization switching
- Cloud-hosted stores and synchronization
- Paid/web search integration
- Hosted agent/plugin marketplace behavior

Still valuable future local-first work:

- MCP server for local agent integrations
- Hybrid lexical + semantic reranking
- Larger benchmark suite for big repositories
- Local PDF/image indexing
- More complete `.gitignore` edge-case semantics
