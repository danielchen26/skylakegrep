# SPDX-License-Identifier: Apache-2.0
"""Measure skylakegrep retrieval context compression.

This benchmark answers a narrower question than a full agent benchmark:

    How many tokens would an LLM receive if it used skylakegrep top-k retrieval
    instead of reading the whole local corpus?

It does not claim end-to-end agent token savings. A Claude/OpenCode/Codex-style
agent benchmark must also count planning prompts, tool calls, repeated searches,
and final answer tokens. This script gives the retrieval-layer compression that
such an agent benchmark can build on.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Iterable

from skylakegrep.src.cli import render_json_results
from skylakegrep.src.embeddings import get_embedder
from skylakegrep.src.indexer import batch_embed, collect_indexable_files, prepare_file_chunks
from skylakegrep.src.storage import (
    delete_missing_files,
    init_db,
    populate_symbols,
    search,
    store_chunks_batch,
)


DEFAULT_QUERIES = [
    {
        "query": "how does hybrid ranking combine lexical and semantic scores",
        "expected": "skylakegrep/src/storage.py",
    },
    {
        "query": "where are files chunked and embedded during indexing",
        "expected": "skylakegrep/src/indexer.py",
    },
    {
        "query": "how does the CLI expose semantic-only search",
        "expected": "skylakegrep/src/cli.py",
    },
    {
        "query": "how does incremental indexing remove deleted files",
        "expected": "skylakegrep/src/storage.py",
    },
    {
        "query": "how does local answer mode call Ollama",
        "expected": "skylakegrep/src/answerer.py",
    },
    {
        "query": "what tests cover skygrepignore and batch embedding",
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
    "skylakegrep.egg-info",
}


@functools.lru_cache(maxsize=8)
def _resolve_tokenizer(requested: str, encoding_name: str):
    if requested == "chars":
        return None, "chars-per-token", False
    try:
        import tiktoken  # type: ignore

        return tiktoken.get_encoding(encoding_name), f"tiktoken:{encoding_name}", True
    except (ImportError, ValueError) as exc:
        if requested == "tiktoken":
            raise RuntimeError(
                "tiktoken token counting was requested but is unavailable; "
                "install the benchmark extra with `pip install -e '.[benchmark]'`"
            ) from exc
        return None, "chars-per-token-fallback", False


def tokenizer_metadata() -> dict[str, object]:
    requested = os.environ.get("SKYGREP_BENCH_TOKENIZER", "chars").strip().lower()
    if requested not in {"chars", "auto", "tiktoken"}:
        raise ValueError(f"unknown benchmark tokenizer: {requested}")
    encoding_name = os.environ.get("SKYGREP_BENCH_TIKTOKEN_ENCODING", "cl100k_base")
    _, actual, exact = _resolve_tokenizer(requested, encoding_name)
    return {
        "requested": requested,
        "actual": actual,
        "exact_tokenizer": exact,
        "encoding": encoding_name if exact else None,
    }


def approximate_tokens(text: str, chars_per_token: int) -> int:
    """Count tokens with the configured real tokenizer or documented fallback.

    The historical function name is retained for benchmark compatibility.
    Set ``SKYGREP_BENCH_TOKENIZER=tiktoken`` to require exact tokenization or
    ``auto`` to prefer it and fall back to ``chars_per_token``.
    """

    metadata = tokenizer_metadata()
    if metadata["exact_tokenizer"]:
        encoder, _, _ = _resolve_tokenizer(str(metadata["requested"]), str(metadata["encoding"]))
        return max(1, len(encoder.encode(text, disallowed_special=())))
    return max(1, len(text) // chars_per_token)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def count_files(files: Iterable[Path], chars_per_token: int) -> dict[str, int]:
    paths = list(files)
    chars = 0
    lines = 0
    tokens = 0
    for path in paths:
        text = read_text(path)
        chars += len(text)
        lines += text.count("\n") + 1
        tokens += approximate_tokens(text, chars_per_token)
    return {
        "files": len(paths),
        "lines": lines,
        "chars": chars,
        "approx_tokens": max(1, tokens),
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


def _ensure_symbols_populated(conn: sqlite3.Connection, root: Path) -> int:
    """Populate ``symbols`` for the chunks in ``conn`` if it is empty.

    Mirrors the file-graph-build pattern used elsewhere: best-effort,
    silently skips when the table is already non-empty so re-runs of
    the bench don't pay the extraction cost twice. Returns the number
    of rows inserted (0 when the table was already populated). The
    symbol channel relies on this table being non-empty to contribute
    anything; without it, ``multi_channel_search`` silently degrades to
    cosine-only.
    """

    try:
        existing = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    except sqlite3.OperationalError:
        # ``symbols`` table missing — caller should have run ``init_db``
        # first; nothing more we can do here.
        return 0
    if existing > 0:
        return 0
    return populate_symbols(conn, root)


def build_index(
    root: Path,
    db_path: Path,
    batch_size: int,
    reuse_existing: bool = False,
) -> tuple[sqlite3.Connection, float]:
    """Build (or reuse) a chunk index at ``db_path``.

    With ``reuse_existing=True`` and a non-empty index already at ``db_path``,
    skip the embed loop and return a connection to the existing index. This
    lets long-running benchmarks (e.g. Rust workspace) be re-run against a pre-built
    index without paying the multi-minute re-embed cost on every invocation.
    """

    if reuse_existing and db_path.exists():
        conn = sqlite3.connect(db_path)
        existing = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if existing > 0:
            # Re-bench against an already-built index. Make sure the
            # symbols table exists for the symbol channel; mirrors the
            # ``populate_file_embeddings`` / ``populate_graph_table``
            # one-time migration pattern in cli.py.
            _ensure_symbols_populated(conn, root)
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
    elapsed = time.perf_counter() - started
    # Populate the symbols table now so the symbol channel has something
    # to query against. Best-effort: failures here must not hide the
    # successful chunk index.
    try:
        _ensure_symbols_populated(conn, root)
    except Exception:
        pass
    return conn, elapsed


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
    parser = argparse.ArgumentParser(description="Benchmark skylakegrep retrieval token savings.")
    parser.add_argument("--root", default=".", help="Repository or directory to benchmark")
    parser.add_argument("--db-path", help="SQLite path to use; defaults to a temporary file")
    parser.add_argument("--top-k", type=int, default=5, help="Number of retrieved snippets per query")
    parser.add_argument("--batch-size", type=int, default=10, help="Embedding batch size for indexing")
    parser.add_argument("--chars-per-token", type=int, default=4, help="Approximate token conversion")
    parser.add_argument("--tokenizer", choices=["chars", "auto", "tiktoken"], default="chars")
    parser.add_argument("--queries", type=Path, help="JSON file with [{'query': ..., 'expected': ...}] entries")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    os.environ["SKYGREP_BENCH_TOKENIZER"] = args.tokenizer
    root = Path(args.root).resolve()
    db_path = Path(args.db_path) if args.db_path else Path(tempfile.gettempdir()) / "skylakegrep-token-benchmark.sqlite"

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
            "indexed_context_reduction_x": "indexed corpus tokens / retrieved JSON tokens",
            "source_doc_context_reduction_x": "source+docs corpus tokens / retrieved JSON tokens",
            "note": "This measures retrieval-layer context compression, not full agent token usage.",
        },
        "tokenizer": tokenizer_metadata(),
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
