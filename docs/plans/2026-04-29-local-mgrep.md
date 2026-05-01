# local-mgrep Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a free local semantic code search tool using Ollama embeddings - no API quota needed.

**Architecture:** Vector search with SQLite + numpy for storage, Ollama for embeddings, tree-sitter for code parsing. Indexes code files and enables natural language search.

**Tech Stack:** Python, Ollama API, SQLite, numpy, tree-sitter

---

## Task 1: Project Setup

**Files:**
- Create: `local-mgrep/pyproject.toml`
- Create: `local-mgrep/src/__init__.py`
- Create: `local-mgrep/src/config.py`
- Create: `local-mgrep/src/indexer.py`
- Create: `local-mgrep/src/searcher.py`
- Create: `local-mgrep/src/cli.py`
- Create: `local-mgrep/tests/`

**Step 1: Create project structure**

```bash
mkdir -p local-mgrep/src local-mgrep/tests
touch local-mgrep/src/__init__.py
```

**Step 2: Create pyproject.toml**

```toml
[project]
name = "local-mgrep"
version = "0.1.0"
description = "Free local semantic code search using Ollama"
requires-python = ">=3.10"
dependencies = [
    "click>=8.0",
    "requests>=2.31",
    "numpy>=1.26",
    "scikit-learn>=1.4",
    "tree-sitter>=0.21",
    "tree-sitter-languages>=1.10",
]

[project.scripts]
mgrep = "local_mgrep.cli:main"
```

**Step 3: Install dependencies**

Run: `cd local-mgrep && pip install -e .`
Expected: Installation succeeds

---

## Task 2: Config Module

**Files:**
- Create: `local-mgrep/src/config.py`

**Step 1: Write config module**

```python
"""Configuration for local-mgrep."""
import os
from pathlib import Path

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_EMBED_MODEL = "nomic-embed-text:latest"
DEFAULT_DB_PATH = Path.home() / ".local-mgrep" / "index.db"

def get_config():
    return {
        "ollama_url": os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
        "embed_model": os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        "db_path": Path(os.environ.get("MGREP_DB_PATH", str(DEFAULT_DB_PATH))),
    }
```

---

## Task 3: Ollama Embedding Integration

**Files:**
- Create: `local-mgrep/src/embeddings.py`

**Step 1: Write Ollama embedding client**

```python
"""Ollama embedding integration."""
import requests
from .config import get_config

def get_embedder():
    config = get_config()
    return OllamaEmbedder(config["ollama_url"], config["embed_model"])

class OllamaEmbedder:
    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def embed(self, text: str) -> list[float]:
        """Generate embedding for text."""
        response = requests.post(
            f"{self.base_url}/api/embeddings",
            json={"model": self.model, "prompt": text},
            timeout=60
        )
        response.raise_for_status()
        return response.json()["embedding"]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Generate embeddings for multiple texts."""
        return [self.embed(text) for text in texts]
```

---

## Task 4: Code Parser & File Indexer

**Files:**
- Create: `local-mgrep/src/indexer.py`

**Step 1: Write code parser using tree-sitter**

```python
"""Code parsing and file indexing."""
import os
from pathlib import Path
from tree_sitter_languages import get_parser
from .embeddings import get_embedder

SUPPORTED_EXTENSIONS = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".go": "go", ".rs": "rust",
    ".java": "java", ".c": "c", ".cpp": "cpp", ".h": "c",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".kt": "kotlin", ".scala": "scala", ".vue": "vue", ".svelte": "svelte",
}

def extract_code_chunks(content: str, language: str, max_lines: int = 100) -> list[str]:
    """Extract meaningful chunks from code using tree-sitter."""
    parser = get_parser(language)
    tree = parser.parse(bytes(content, "utf8"))

    chunks = []
    def walk(node):
        if node.start_point[0] - node.end_point[0] < max_lines:
            chunk = content.encode()[node.start_byte:node.end_byte].decode("utf8")
            if len(chunk.splitlines()) >= 3:
                chunks.append(chunk)
        for child in node.children:
            walk(child)

    walk(tree.root_node)
    return chunks or [content[:2000]]

def index_file(filepath: Path, embedder) -> list[dict]:
    """Index a single file, returning chunks with embeddings."""
    ext = filepath.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        return []

    try:
        content = filepath.read_text(errors="ignore")
    except Exception:
        return []

    lang = SUPPORTED_EXTENSIONS[ext]
    chunks = extract_code_chunks(content, lang)

    results = []
    for i, chunk in enumerate(chunks):
        embedding = embedder.embed(chunk)
        results.append({
            "file": str(filepath),
            "chunk": chunk,
            "language": lang,
            "chunk_index": i,
            "embedding": embedding,
        })
    return results
```

---

## Task 5: Vector Storage (SQLite + numpy)

**Files:**
- Modify: `local-mgrep/src/storage.py` (new file)

**Step 1: Write storage module**

```python
"""Vector storage using SQLite + numpy."""
import sqlite3
import numpy as np
from pathlib import Path
from .config import get_config

def init_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            file TEXT, chunk TEXT, language TEXT, chunk_index INTEGER
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS vectors (id INTEGER, embedding BLOB)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_file ON chunks(file)")
    conn.commit()
    return conn

def store_chunk(conn, file: str, chunk: str, language: str, chunk_index: int, embedding: list[float]):
    cursor = conn.execute(
        "INSERT INTO chunks (file, chunk, language, chunk_index) VALUES (?, ?, ?, ?)",
        (file, chunk, language, chunk_index)
    )
    vec = np.array(embedding, dtype=np.float32)
    conn.execute("INSERT INTO vectors (id, embedding) VALUES (?, ?)",
                 (cursor.lastrowid, vec.tobytes()))
    conn.commit()

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8)

def search(conn, query_embedding: list[float], top_k: int = 10) -> list[dict]:
    query_vec = np.array(query_embedding, dtype=np.float32)
    cursor = conn.execute("SELECT id, embedding FROM vectors")
    results = []
    for row in cursor:
        vec = np.frombuffer(row[1], dtype=np.float32)
        score = cosine_similarity(query_vec, vec)
        results.append((score, row[0]))
    results.sort(reverse=True)
    chunk_ids = [r[1] for r in results[:top_k]]
    if not chunk_ids:
        return []
    placeholders = ",".join("?" * len(chunk_ids))
    chunks = conn.execute(
        f"SELECT id, file, chunk, language FROM chunks WHERE id IN ({placeholders})",
        chunk_ids
    ).fetchall()
    id_to_chunk = {c[0]: c for c in chunks}
    return [{"id": r[1], "file": id_to_chunk[r[1]][1], "chunk": id_to_chunk[r[1]][2], "score": r[0]}
            for r in results[:top_k] if r[1] in id_to_chunk]
```

---

## Task 6: CLI Interface

**Files:**
- Create: `local-mgrep/src/cli.py`

**Step 1: Write CLI module**

```python
"""CLI for local-mgrep."""
import click
from pathlib import Path
from .indexer import index_file, SUPPORTED_EXTENSIONS
from .embeddings import get_embedder
from .storage import init_db, store_chunk, search
from .config import get_config

@click.group()
def cli():
    """Local semantic code search - free, no quota."""
    pass

@cli.command()
@click.argument("path", default=".")
@click.option("--reset", is_flag=True, help="Reset existing index")
def index(path: str, reset: bool):
    """Index a codebase for searching."""
    config = get_config()
    db_path = config["db_path"]

    if reset and db_path.exists():
        db_path.unlink()

    conn = init_db(db_path)
    embedder = get_embedder()

    files = []
    for ext in SUPPORTED_EXTENSIONS:
        files.extend(Path(path).rglob(f"*{ext}"))

    click.echo(f"Indexing {len(files)} files...")
    for f in files:
        chunks = index_file(f, embedder)
        for chunk in chunks:
            store_chunk(conn, chunk["file"], chunk["chunk"], chunk["language"], chunk["chunk_index"], chunk["embedding"])
        click.echo(f"  Indexed: {f}")

    click.echo("Indexing complete!")

@cli.command()
@click.argument("query")
@click.option("--top", "-n", default=5, help="Number of results")
def search_cmd(query: str, top: int):
    """Search the index with natural language."""
    config = get_config()
    conn = sqlite3.connect(config["db_path"])
    embedder = get_embedder()

    query_embedding = embedder.embed(query)
    results = search(conn, query_embedding, top)

    for r in results:
        click.echo(f"\n=== {r['file']} (score: {r['score']:.3f}) ===")
        click.echo(r["chunk"][:500])

if __name__ == "__main__":
    main()
```

**Step 2: Fix missing import in cli.py**

Add at top:
```python
import sqlite3
```

---

## Task 7: Create CLI entrypoint

**Files:**
- Modify: `local-mgrep/src/cli.py` - fix main()

**Step 1: Update main function**

```python
def main():
    cli()
```

---

## Task 8: Testing

**Files:**
- Create: `local-mgrep/tests/test_indexer.py`

**Step 1: Write basic test**

```python
"""Tests for local-mgrep."""
from local_mgrep.src.indexer import SUPPORTED_EXTENSIONS

def test_supported_extensions():
    assert ".py" in SUPPORTED_EXTENSIONS
    assert ".js" in SUPPORTED_EXTENSIONS

def test_extract_code_chunks():
    from local_mgrep.src.indexer import extract_code_chunks
    code = "def foo():\n    pass\ndef bar():\n    pass"
    chunks = extract_code_chunks(code, "python")
    assert len(chunks) > 0
```

**Step 2: Run tests**

Run: `cd local-mgrep && python -m pytest tests/ -v`
Expected: Tests pass

---

## Execution Options

**1. Subagent-Driven (this session)** - I dispatch fresh subagent per task, review between tasks, fast iteration

**2. Parallel Session (separate)** - Open new session with executing-plans, batch execution with checkpoints

**Which approach?**