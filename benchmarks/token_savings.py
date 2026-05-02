"""Measure local-mgrep retrieval context compression.

This benchmark answers a narrower question than a full agent benchmark:

    How many tokens would an LLM receive if it used local-mgrep top-k retrieval
    instead of reading the whole local corpus?

It does not claim end-to-end agent token savings. A Claude/OpenCode/Codex-style
agent benchmark must also count planning prompts, tool calls, repeated searches,
and final answer tokens. This script gives the retrieval-layer compression that
such an agent benchmark can build on.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Iterable

from local_mgrep.src.cli import render_json_results
from local_mgrep.src.embeddings import get_embedder
from local_mgrep.src.indexer import batch_embed, collect_indexable_files, prepare_file_chunks
from local_mgrep.src.storage import delete_missing_files, init_db, search, store_chunks_batch


DEFAULT_QUERIES = [
    {
        "query": "how does hybrid ranking combine lexical and semantic scores",
        "expected": "local_mgrep/src/storage.py",
    },
    {
        "query": "where are files chunked and embedded during indexing",
        "expected": "local_mgrep/src/indexer.py",
    },
    {
        "query": "how does the CLI expose semantic-only search",
        "expected": "local_mgrep/src/cli.py",
    },
    {
        "query": "how does incremental indexing remove deleted files",
        "expected": "local_mgrep/src/storage.py",
    },
    {
        "query": "how does local answer mode call Ollama",
        "expected": "local_mgrep/src/answerer.py",
    },
    {
        "query": "what tests cover mgrepignore and batch embedding",
        "expected": "tests/test_parity_batch.py",
    },
]

DEFAULT_SOURCE_DOC_SUFFIXES = {".py", ".md", ".toml"}
DEFAULT_IGNORED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "benchmarks",
    "build",
    "dist",
    "local_mgrep.egg-info",
}


def approximate_tokens(text: str, chars_per_token: int) -> int:
    return max(1, len(text) // chars_per_token)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def count_files(files: Iterable[Path], chars_per_token: int) -> dict[str, int]:
    paths = list(files)
    chars = 0
    lines = 0
    for path in paths:
        text = read_text(path)
        chars += len(text)
        lines += text.count("\n") + 1
    return {
        "files": len(paths),
        "lines": lines,
        "chars": chars,
        "approx_tokens": max(1, chars // chars_per_token),
    }


def collect_source_doc_files(root: Path, suffixes: set[str]) -> list[Path]:
    files = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        relative = path.relative_to(root)
        if any(part in DEFAULT_IGNORED_PARTS for part in relative.parts):
            continue
        files.append(path)
    return sorted(files)


def is_benchmark_ignored(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    return any(part in DEFAULT_IGNORED_PARTS for part in relative.parts)


def build_index(
    root: Path,
    db_path: Path,
    batch_size: int,
    reuse_existing: bool = False,
) -> tuple[sqlite3.Connection, float]:
    """Build (or reuse) a chunk index at ``db_path``.

    With ``reuse_existing=True`` and a non-empty index already at ``db_path``,
    skip the embed loop and return a connection to the existing index. This
    lets long-running benchmarks (e.g. warp) be re-run against a pre-built
    index without paying the multi-minute re-embed cost on every invocation.
    """

    if reuse_existing and db_path.exists():
        conn = sqlite3.connect(db_path)
        existing = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if existing > 0:
            return conn, 0.0
        conn.close()
    if db_path.exists():
        db_path.unlink()
    conn = init_db(db_path)
    embedder = get_embedder()
    files = [path for path in collect_indexable_files(root) if not is_benchmark_ignored(path, root)]
    delete_missing_files(conn, {str(path) for path in files}, root)

    started = time.perf_counter()
    for path in files:
        chunks = prepare_file_chunks(path, root=root)
        if not chunks:
            continue
        relative = path.relative_to(root).as_posix()
        for chunk in chunks:
            chunk["file"] = relative
        store_chunks_batch(conn, batch_embed(chunks, embedder, batch_size=batch_size))
    return conn, time.perf_counter() - started


def run_queries(
    conn: sqlite3.Connection,
    queries: list[dict[str, str]],
    top_k: int,
    chars_per_token: int,
) -> list[dict[str, object]]:
    embedder = get_embedder()
    rows = []
    for item in queries:
        query = item["query"]
        expected = item.get("expected", "")
        started = time.perf_counter()
        results = search(
            conn,
            embedder.embed(query),
            top_k=top_k,
            query_text=query,
        )
        latency = time.perf_counter() - started
        payload = render_json_results(results)
        paths = [result["path"] for result in results]
        rows.append(
            {
                "query": query,
                "expected": expected,
                "top_path": paths[0] if paths else None,
                "expected_in_top_k": expected in paths if expected else None,
                "result_count": len(results),
                "retrieval_chars": len(payload),
                "retrieval_approx_tokens": approximate_tokens(payload, chars_per_token),
                "latency_seconds": round(latency, 3),
            }
        )
    return rows


def load_queries(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return DEFAULT_QUERIES
    return json.loads(path.read_text(encoding="utf-8"))


def summarize(rows: list[dict[str, object]], indexed_tokens: int, source_doc_tokens: int) -> dict[str, object]:
    enriched = []
    for row in rows:
        retrieval_tokens = int(row["retrieval_approx_tokens"])
        enriched.append(
            {
                **row,
                "indexed_context_reduction_x": round(indexed_tokens / retrieval_tokens, 1),
                "source_doc_context_reduction_x": round(source_doc_tokens / retrieval_tokens, 1),
            }
        )
    hit_values = [row["expected_in_top_k"] for row in enriched if row["expected_in_top_k"] is not None]
    return {
        "queries": enriched,
        "averages": {
            "retrieval_approx_tokens": round(
                sum(int(row["retrieval_approx_tokens"]) for row in enriched) / len(enriched), 1
            ),
            "indexed_context_reduction_x": round(
                sum(float(row["indexed_context_reduction_x"]) for row in enriched) / len(enriched), 1
            ),
            "source_doc_context_reduction_x": round(
                sum(float(row["source_doc_context_reduction_x"]) for row in enriched) / len(enriched), 1
            ),
            "latency_seconds": round(
                sum(float(row["latency_seconds"]) for row in enriched) / len(enriched), 3
            ),
            "expected_top_k_hit_rate": f"{sum(1 for value in hit_values if value)}/{len(hit_values)}",
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark local-mgrep retrieval token savings.")
    parser.add_argument("--root", default=".", help="Repository or directory to benchmark")
    parser.add_argument("--db-path", help="SQLite path to use; defaults to a temporary file")
    parser.add_argument("--top-k", type=int, default=5, help="Number of retrieved snippets per query")
    parser.add_argument("--batch-size", type=int, default=10, help="Embedding batch size for indexing")
    parser.add_argument("--chars-per-token", type=int, default=4, help="Approximate token conversion")
    parser.add_argument("--queries", type=Path, help="JSON file with [{'query': ..., 'expected': ...}] entries")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    root = Path(args.root).resolve()
    db_path = Path(args.db_path) if args.db_path else Path(tempfile.gettempdir()) / "local-mgrep-token-benchmark.sqlite"

    indexable_files = [path for path in collect_indexable_files(root) if not is_benchmark_ignored(path, root)]
    indexed_corpus = count_files(indexable_files, args.chars_per_token)
    source_doc_corpus = count_files(
        collect_source_doc_files(root, DEFAULT_SOURCE_DOC_SUFFIXES),
        args.chars_per_token,
    )
    conn, index_seconds = build_index(root, db_path, batch_size=args.batch_size)
    chunks, indexed_db_files = conn.execute("SELECT COUNT(*), COUNT(DISTINCT file) FROM chunks").fetchone()
    query_rows = run_queries(conn, load_queries(args.queries), args.top_k, args.chars_per_token)

    report = {
        "definition": {
            "indexed_context_reduction_x": "indexed corpus approx tokens / retrieved JSON approx tokens",
            "source_doc_context_reduction_x": "source+docs corpus approx tokens / retrieved JSON approx tokens",
            "note": "This measures retrieval-layer context compression, not full agent token usage.",
        },
        "index": {
            "seconds": round(index_seconds, 3),
            "db_path": str(db_path),
            "indexed_db_files": indexed_db_files,
            "chunks": chunks,
            "indexed_corpus": indexed_corpus,
            "source_doc_corpus": source_doc_corpus,
        },
        **summarize(
            query_rows,
            indexed_tokens=indexed_corpus["approx_tokens"],
            source_doc_tokens=source_doc_corpus["approx_tokens"],
        ),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
