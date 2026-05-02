"""Deterministic grep-agent vs local-mgrep-agent benchmark.

This benchmark is closer to the original mgrep token-savings claim than
`token_savings.py`, but it is still intentionally local and deterministic. It
does not call Claude/OpenCode/Codex or read provider billing meters. Instead, it
compares two context-gathering policies an agent could use before answering:

1. grep-agent: issue several exact term searches and pass matching line windows.
2. mgrep-agent: issue one local-mgrep semantic search and pass top-k snippets.

The benchmark reports context-only token ratios and estimated end-to-end ratios
after adding configurable fixed prompt/output overhead. This approximates the
shape of an agent workflow while staying reproducible and free/local.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from local_mgrep.src.cli import render_json_results
from local_mgrep.src.embeddings import get_embedder
from local_mgrep.src.storage import search

from benchmarks.token_savings import (
    DEFAULT_IGNORED_PARTS,
    approximate_tokens,
    build_index,
    collect_source_doc_files,
    count_files,
    is_benchmark_ignored,
)
from local_mgrep.src.indexer import collect_indexable_files


DEFAULT_TASKS = [
    {
        "id": "hybrid-ranking-001",
        "question": "Where are lexical and semantic scores combined for ranking?",
        "expected": "local_mgrep/src/storage.py",
    },
    {
        "id": "hybrid-ranking-002",
        "question": "How is query text tokenized for exact code-term matching?",
        "expected": "local_mgrep/src/storage.py",
    },
    {
        "id": "semantic-only-001",
        "question": "Which CLI option disables lexical reranking and keeps pure vector ordering?",
        "expected": "local_mgrep/src/cli.py",
    },
    {
        "id": "json-output-001",
        "question": "Where is the stable JSON result schema created for agents?",
        "expected": "local_mgrep/src/cli.py",
    },
    {
        "id": "agentic-001",
        "question": "How does search split a broad question into local subqueries?",
        "expected": "local_mgrep/src/cli.py",
    },
    {
        "id": "answer-mode-001",
        "question": "Where does answer mode synthesize an answer from retrieved snippets?",
        "expected": "local_mgrep/src/cli.py",
    },
    {
        "id": "watch-mode-001",
        "question": "Where does watch mode update changed files in the index?",
        "expected": "local_mgrep/src/cli.py",
    },
    {
        "id": "watch-mode-002",
        "question": "Where does watch mode remove records for deleted files?",
        "expected": "local_mgrep/src/cli.py",
    },
    {
        "id": "indexing-001",
        "question": "Where are indexable source files collected while respecting ignore rules?",
        "expected": "local_mgrep/src/indexer.py",
    },
    {
        "id": "indexing-002",
        "question": "Where are .gitignore and .mgrepignore patterns loaded?",
        "expected": "local_mgrep/src/indexer.py",
    },
    {
        "id": "indexing-003",
        "question": "Where are vendor and generated directories skipped by default?",
        "expected": "local_mgrep/src/indexer.py",
    },
    {
        "id": "chunking-001",
        "question": "Where does the fallback splitter create line and byte ranges?",
        "expected": "local_mgrep/src/indexer.py",
    },
    {
        "id": "chunking-002",
        "question": "Where does tree-sitter extraction fall back to text chunking?",
        "expected": "local_mgrep/src/indexer.py",
    },
    {
        "id": "chunking-003",
        "question": "Where are per-file chunks prepared with language and mtime metadata?",
        "expected": "local_mgrep/src/indexer.py",
    },
    {
        "id": "embedding-001",
        "question": "Where does indexing use a batch embedding API when available?",
        "expected": "local_mgrep/src/indexer.py",
    },
    {
        "id": "embedding-002",
        "question": "Where does the Ollama embedder call the batch endpoint and fallback endpoint?",
        "expected": "local_mgrep/src/embeddings.py",
    },
    {
        "id": "answerer-001",
        "question": "Where does local answer mode call an Ollama chat model?",
        "expected": "local_mgrep/src/answerer.py",
    },
    {
        "id": "answerer-002",
        "question": "Where are generated subqueries parsed from a local model response?",
        "expected": "local_mgrep/src/answerer.py",
    },
    {
        "id": "storage-001",
        "question": "Where is the SQLite chunk schema initialized with provenance columns?",
        "expected": "local_mgrep/src/storage.py",
    },
    {
        "id": "storage-002",
        "question": "Where are deleted file chunks removed from both tables?",
        "expected": "local_mgrep/src/storage.py",
    },
    {
        "id": "storage-003",
        "question": "Where does incremental cleanup remove rows for missing files?",
        "expected": "local_mgrep/src/storage.py",
    },
    {
        "id": "storage-004",
        "question": "Where are include and exclude path filters applied?",
        "expected": "local_mgrep/src/storage.py",
    },
    {
        "id": "storage-005",
        "question": "Where does vectorized scoring rank chunks with NumPy?",
        "expected": "local_mgrep/src/storage.py",
    },
    {
        "id": "storage-006",
        "question": "Where are duplicate logical search results skipped?",
        "expected": "local_mgrep/src/storage.py",
    },
    {
        "id": "config-001",
        "question": "Where are Ollama and database environment variables read?",
        "expected": "local_mgrep/src/config.py",
    },
    {
        "id": "tests-001",
        "question": "Which test covers stable JSON output schema?",
        "expected": "tests/test_search_quality.py",
    },
    {
        "id": "tests-002",
        "question": "Which test covers .mgrepignore behavior?",
        "expected": "tests/test_parity_batch.py",
    },
    {
        "id": "tests-003",
        "question": "Which test proves batch embedding is used for indexing speed?",
        "expected": "tests/test_parity_batch.py",
    },
    {
        "id": "tests-004",
        "question": "Which test proves semantic-only disables lexical boosting?",
        "expected": "tests/test_hybrid_ranking.py",
    },
    {
        "id": "tests-005",
        "question": "Which test covers local answer synthesis from search results?",
        "expected": "tests/test_answer_mode.py",
    },
]

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "by",
    "does",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "or",
    "the",
    "to",
    "what",
    "where",
    "which",
    "with",
}


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def tokenize(text: str) -> list[str]:
    terms = []
    for token in TOKEN_RE.findall(text.lower()):
        parts = [part for part in re.split(r"[-_]", token) if part]
        terms.append(token)
        terms.extend(parts)
    return [term for term in terms if len(term) > 2 and term not in STOPWORDS]


def term_variants(term: str) -> list[str]:
    variants = [term]
    for suffix in ("ing", "ed", "es", "s"):
        if term.endswith(suffix) and len(term) > len(suffix) + 2:
            variants.append(term[: -len(suffix)])
    return list(dict.fromkeys(variants))


def extract_terms(question: str, max_terms: int) -> list[str]:
    counts = Counter(tokenize(question))
    ordered = sorted(counts, key=lambda term: (-len(term), term))
    expanded = []
    for term in ordered:
        for variant in term_variants(term):
            if variant not in expanded:
                expanded.append(variant)
        if len(expanded) >= max_terms:
            break
    return expanded[:max_terms]


def context_window(lines: list[str], line_index: int, radius: int) -> tuple[int, int, str]:
    start = max(0, line_index - radius)
    end = min(len(lines), line_index + radius + 1)
    numbered = [f"{index + 1}: {lines[index]}" for index in range(start, end)]
    return start + 1, end, "\n".join(numbered)


def grep_agent_context(
    question: str,
    files: list[Path],
    root: Path,
    max_terms: int,
    max_matches_per_term: int,
    context_lines: int,
    chars_per_token: int,
) -> dict[str, object]:
    started = time.perf_counter()
    terms = extract_terms(question, max_terms=max_terms)
    sections = []
    paths_seen = set()
    tool_calls = 0

    for term in terms:
        tool_calls += 1
        matches = []
        lowered_term = term.lower()
        for path in files:
            relative = path.relative_to(root).as_posix()
            text = read_text(path)
            lines = text.splitlines()
            path_matches = lowered_term in relative.lower()
            for index, line in enumerate(lines):
                if not path_matches and lowered_term not in line.lower():
                    continue
                start, end, snippet = context_window(lines, index, context_lines)
                matches.append((relative, start, end, snippet))
                paths_seen.add(relative)
                if len(matches) >= max_matches_per_term:
                    break
            if len(matches) >= max_matches_per_term:
                break
        if matches:
            section_lines = [f"## grep term: {term}"]
            for relative, start, end, snippet in matches:
                section_lines.append(f"### {relative}:{start}-{end}\n{snippet}")
            sections.append("\n".join(section_lines))

    payload = "\n\n".join(sections) if sections else "NO_MATCHES"
    return {
        "tool_calls": tool_calls,
        "paths": sorted(paths_seen),
        "context_chars": len(payload),
        "context_tokens": approximate_tokens(payload, chars_per_token),
        "latency_seconds": round(time.perf_counter() - started, 3),
    }


def mgrep_agent_context(
    conn: sqlite3.Connection,
    question: str,
    top_k: int,
    chars_per_token: int,
    rerank: bool = True,
    rerank_pool: int = 50,
    hyde: bool = False,
    multi_resolution: bool = False,
    file_top: int = 30,
    daemon_url: str | None = None,
) -> dict[str, object]:
    if daemon_url:
        from local_mgrep.src.server import daemon_search

        started = time.perf_counter()
        resp = daemon_search(
            daemon_url,
            question,
            top_k=top_k,
            rerank=rerank,
            rerank_pool=rerank_pool,
            multi_resolution=multi_resolution,
            file_top=file_top,
            hyde=hyde,
        )
        results = resp.get("results", [])
        payload = json.dumps(results, indent=2)
        latency = time.perf_counter() - started
        return {
            "tool_calls": 1,
            "paths": [r["path"] for r in results],
            "context_chars": len(payload),
            "context_tokens": approximate_tokens(payload, chars_per_token),
            "latency_seconds": round(latency, 3),
        }

    embedder = get_embedder(role="query") if "role" in get_embedder.__code__.co_varnames else get_embedder()
    started = time.perf_counter()
    if hyde:
        try:
            from local_mgrep.src.answerer import get_answerer
            embed_input = get_answerer().hyde(question)
        except Exception:
            embed_input = question
    else:
        embed_input = question
    results = search(
        conn,
        embedder.embed(embed_input),
        top_k=top_k,
        query_text=question,
        rerank=rerank,
        rerank_pool=rerank_pool,
        multi_resolution=multi_resolution,
        file_top=file_top,
    )
    payload = render_json_results(results)
    return {
        "tool_calls": 1,
        "paths": [result["path"] for result in results],
        "context_chars": len(payload),
        "context_tokens": approximate_tokens(payload, chars_per_token),
        "latency_seconds": round(time.perf_counter() - started, 3),
    }


def load_tasks(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return DEFAULT_TASKS
    return json.loads(path.read_text(encoding="utf-8"))


def safe_ratio(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 2)


def _expected_hit(expected: str, paths: list[str]) -> bool:
    """Substring match between an ``expected`` path and the returned paths.

    Returned paths may be absolute (from ``mgrep index``) or repo-relative
    (from the bench's own ``build_index`` rewrite); ``expected`` is repo-
    relative. A substring check covers both cases and matches the convention
    used by ``parity_vs_ripgrep.expected_hit``.
    """

    return any(expected in path for path in paths)


def benchmark(args: argparse.Namespace) -> dict[str, object]:
    root = Path(args.root).resolve()
    db_path = Path(args.db_path) if args.db_path else Path(tempfile.gettempdir()) / "local-mgrep-agent-benchmark.sqlite"
    indexed_files = [path for path in collect_indexable_files(root) if not is_benchmark_ignored(path, root)]
    source_doc_corpus = count_files(
        collect_source_doc_files(root, {".py", ".md", ".toml"}),
        args.chars_per_token,
    )
    indexed_corpus = count_files(indexed_files, args.chars_per_token)
    conn, index_seconds = build_index(
        root,
        db_path,
        batch_size=args.batch_size,
        reuse_existing=getattr(args, "reuse_index", False),
    )
    chunks, indexed_db_files = conn.execute("SELECT COUNT(*), COUNT(DISTINCT file) FROM chunks").fetchone()

    rows = []
    for task in load_tasks(args.tasks):
        expected = task["expected"]
        grep_result = grep_agent_context(
            task["question"],
            indexed_files,
            root,
            max_terms=args.grep_max_terms,
            max_matches_per_term=args.grep_max_matches_per_term,
            context_lines=args.grep_context_lines,
            chars_per_token=args.chars_per_token,
        )
        mgrep_result = mgrep_agent_context(
            conn,
            task["question"],
            top_k=args.top_k,
            chars_per_token=args.chars_per_token,
            rerank=getattr(args, "rerank", True),
            rerank_pool=getattr(args, "rerank_pool", 50),
            hyde=getattr(args, "hyde", False),
            multi_resolution=getattr(args, "multi_resolution", False),
            file_top=getattr(args, "file_top", 30),
            daemon_url=getattr(args, "daemon_url", None),
        )
        grep_total = args.fixed_prompt_tokens + args.final_answer_tokens + int(grep_result["context_tokens"])
        mgrep_total = args.fixed_prompt_tokens + args.final_answer_tokens + int(mgrep_result["context_tokens"])
        rows.append(
            {
                "id": task["id"],
                "question": task["question"],
                "expected": expected,
                "grep": {
                    **grep_result,
                    "hit": _expected_hit(expected, grep_result["paths"]),
                    "estimated_total_tokens": grep_total,
                },
                "mgrep": {
                    **mgrep_result,
                    "hit": _expected_hit(expected, mgrep_result["paths"]),
                    "estimated_total_tokens": mgrep_total,
                },
                "context_token_reduction_x": safe_ratio(
                    float(grep_result["context_tokens"]), float(mgrep_result["context_tokens"])
                ),
                "estimated_total_token_reduction_x": safe_ratio(grep_total, mgrep_total),
            }
        )

    grep_context = sum(int(row["grep"]["context_tokens"]) for row in rows)
    mgrep_context = sum(int(row["mgrep"]["context_tokens"]) for row in rows)
    grep_total = sum(int(row["grep"]["estimated_total_tokens"]) for row in rows)
    mgrep_total = sum(int(row["mgrep"]["estimated_total_tokens"]) for row in rows)
    return {
        "definition": {
            "benchmark_type": "deterministic context-gathering agent simulation",
            "grep_agent": "multiple exact term searches over indexed files, returning matching line windows",
            "mgrep_agent": "one semantic local-mgrep top-k search per task",
            "token_note": "tokens are approximate chars/4; estimated totals add fixed prompt and final-answer overhead",
        },
        "parameters": {
            "tasks": len(rows),
            "top_k": args.top_k,
            "grep_max_terms": args.grep_max_terms,
            "grep_max_matches_per_term": args.grep_max_matches_per_term,
            "grep_context_lines": args.grep_context_lines,
            "fixed_prompt_tokens": args.fixed_prompt_tokens,
            "final_answer_tokens": args.final_answer_tokens,
        },
        "index": {
            "seconds": round(index_seconds, 3),
            "db_path": str(db_path),
            "indexed_db_files": indexed_db_files,
            "chunks": chunks,
            "indexed_corpus": indexed_corpus,
            "source_doc_corpus": source_doc_corpus,
        },
        "summary": {
            "grep_context_tokens": grep_context,
            "mgrep_context_tokens": mgrep_context,
            "context_token_reduction_x": safe_ratio(grep_context, mgrep_context),
            "grep_estimated_total_tokens": grep_total,
            "mgrep_estimated_total_tokens": mgrep_total,
            "estimated_total_token_reduction_x": safe_ratio(grep_total, mgrep_total),
            "grep_hit_rate": f"{sum(1 for row in rows if row['grep']['hit'])}/{len(rows)}",
            "mgrep_hit_rate": f"{sum(1 for row in rows if row['mgrep']['hit'])}/{len(rows)}",
            "grep_avg_latency_seconds": round(
                sum(float(row["grep"]["latency_seconds"]) for row in rows) / len(rows), 3
            ),
            "mgrep_avg_latency_seconds": round(
                sum(float(row["mgrep"]["latency_seconds"]) for row in rows) / len(rows), 3
            ),
            "grep_tool_calls": sum(int(row["grep"]["tool_calls"]) for row in rows),
            "mgrep_tool_calls": sum(int(row["mgrep"]["tool_calls"]) for row in rows),
        },
        "tasks": rows,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark grep-agent vs local-mgrep-agent context usage.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--db-path")
    parser.add_argument("--tasks", type=Path, help="Optional JSON task list")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--chars-per-token", type=int, default=4)
    parser.add_argument("--grep-max-terms", type=int, default=8)
    parser.add_argument("--grep-max-matches-per-term", type=int, default=20)
    parser.add_argument("--grep-context-lines", type=int, default=2)
    parser.add_argument("--fixed-prompt-tokens", type=int, default=1000)
    parser.add_argument("--final-answer-tokens", type=int, default=300)
    parser.add_argument("--summary-only", action="store_true", help="Only print definition, parameters, index, and summary")
    parser.add_argument("--rerank", dest="rerank", action="store_true", default=True, help="Apply cross-encoder rerank as second stage (default on)")
    parser.add_argument("--no-rerank", dest="rerank", action="store_false", help="Disable cross-encoder rerank (cosine + lexical only)")
    parser.add_argument("--rerank-pool", type=int, default=50, help="Candidate pool size before reranking")
    parser.add_argument(
        "--reuse-index",
        action="store_true",
        help="Reuse the existing DB at --db-path instead of rebuilding (skips re-embedding)",
    )
    parser.add_argument(
        "--hyde",
        dest="hyde",
        action="store_true",
        default=False,
        help="HyDE: generate a hypothetical code answer with the local LLM and embed that instead of the raw question",
    )
    parser.add_argument(
        "--no-hyde",
        dest="hyde",
        action="store_false",
        help="Disable HyDE (default off)",
    )
    parser.add_argument(
        "--multi-resolution",
        dest="multi_resolution",
        action="store_true",
        default=True,
        help="Two-stage retrieval: file-level cosine top-N then chunk-level (default on)",
    )
    parser.add_argument(
        "--no-multi-resolution",
        dest="multi_resolution",
        action="store_false",
        help="Disable two-stage retrieval; chunk-level cosine over the whole index",
    )
    parser.add_argument("--file-top", dest="file_top", type=int, default=30, help="Number of files surfaced by the file-level stage")
    parser.add_argument("--daemon-url", dest="daemon_url", default=None, help="If set, route every mgrep search through a running mgrep daemon at this URL (skips per-query reranker cold load)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = benchmark(args)
    if args.summary_only:
        report = {key: report[key] for key in ("definition", "parameters", "index", "summary")}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
