# local-mgrep

Free, local semantic code search powered by a local Ollama embedding model.

## Features

* **Semantic search** – find code by describing what it does, not just by keywords.  
* **Tree‑sitter based chunking** – respects language syntax, works for 15+ languages (Python, JavaScript, TypeScript, Go, Rust, Java, C/C++, C#, Ruby, PHP, Swift, Kotlin, Scala, Vue, Svelte).  
* **Fully local** – no API keys, no external quotas; only requires a running Ollama instance.  
* **Incremental indexing** – only re‑indexes changed files.  
* **Watch mode** – keep the index up‑to‑date while you edit.

## Prerequisites

1. **Ollama** (≥ 0.1.x) installed and running: https://ollama.com  
2. Pull an embedding model, e.g.:

   ```bash
   ollama pull nomic-embed-text   # or mxbai-embed-large
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
| `MGREP_DB_PATH` | `~/.local-mgrep/index.db` | SQLite file that stores the vectors and metadata |

## License

MIT – see the `LICENSE` file.

## Acknowledgments

* Uses **tree‑sitter** for language‑aware parsing.  
* Embedding model served by **Ollama**.  
* Built with **Click**, **NumPy**, **Scikit‑learn**.

