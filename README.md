# local-mgrep

Free, local semantic code search powered by a local Ollama embedding model.

For the complete 0.2.0 capability guide and original-`mgrep` parity notes, see
[`docs/local-mgrep-0.2.0.md`](docs/local-mgrep-0.2.0.md).

## Features

* **Semantic search** – find code by describing what it does, not just by keywords.  
* **Tree‑sitter based chunking** – respects language syntax, works for 15+ languages (Python, JavaScript, TypeScript, Go, Rust, Java, C/C++, C#, Ruby, PHP, Swift, Kotlin, Scala, Vue, Svelte).  
* **Fully local** – no API keys, no external quotas; only requires a running Ollama instance.  
* **Incremental indexing** – only re‑indexes changed files.  
* **Watch mode** – keep the index up‑to‑date while you edit.
* **Result provenance** – search output includes source line ranges, and JSON output is available for agents/scripts.
* **Index hygiene** – respects `.gitignore`, `.mgrepignore`, and common generated/vendor directories such as `node_modules`, `dist`, `build`, and `.venv`.
* **Local answer mode** – synthesize answers from local snippets with an Ollama model, no paid API required.
* **Local agentic mode** – split broad questions into bounded local subqueries using Ollama, then merge/deduplicate results.
* **Practical parity flags** – supports `-m`, `--content/--no-content`, `--language`, `--include`, and `--exclude`.

## Prerequisites

1. **Ollama** (≥ 0.1.x) installed and running: https://ollama.com  
2. Pull an embedding model, e.g.:

   ```bash
   ollama pull nomic-embed-text   # or mxbai-embed-large
   ollama pull qwen2.5:3b         # optional, for local --answer synthesis
   ```

3. Python ≥ 3.9 (the package is tested on 3.9‑3.13).

## Installation

### From source (recommended for development)

```bash
git clone https://github.com/yourusername/local-mgrep.git
cd local-mgrep
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .            # editable install
```

### Via pip (once published to PyPI)

```bash
pip install local-mgrep
```

## Usage

```bash
# (first time) build the index for a directory
mgrep index /path/to/your/code --reset

# Search using natural language
mgrep search "how does the authentication work"

# Limit/filter output using original-mgrep-style local flags
mgrep search "auth token" -m 10 --language python --include "src/*" --exclude "*_test.py"

# Emit stable JSON for agents and scripts
mgrep search "how does the authentication work" --json

# Synthesize a local answer from retrieved snippets (uses OLLAMA_LLM_MODEL)
mgrep search "how does the authentication work" --answer

# Use local bounded multi-query search for broad questions
mgrep search "how are tokens created and refreshed" --agentic --answer

# Show index statistics
mgrep stats

# Keep the index up‑to‑date while you work
mgrep watch /path/to/your/code
```

## Configuration (environment variables)

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_URL` | `http://localhost:11434` | Base URL of the Ollama server |
| `OLLAMA_EMBED_MODEL` | `mxbai-embed-large` | Model used for embeddings (`nomic-embed-text`, `mxbai-embed-large`, …) |
| `OLLAMA_LLM_MODEL` | `qwen2.5:3b` | Local Ollama model used for `mgrep search --answer` |
| `MGREP_DB_PATH` | `~/.local-mgrep/index.db` | SQLite file that stores the vectors and metadata |

## License

MIT – see the `LICENSE` file.

## Acknowledgments

* Uses **tree‑sitter** for language‑aware parsing.  
* Embedding model served by **Ollama**.  
* Built with **Click**, **NumPy**, **Scikit‑learn**.
