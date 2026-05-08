"""Multi-HyDE query expansion probe.

Insight: HyDE generates one hypothetical answer per query. If the LLM picks
the wrong identifier vocabulary on the first try (e.g. "neural model"
instead of "openai client"), HyDE adds noise rather than signal. Generating
N independent hypothetical docs with different framings then searching each
might catch the canonical file via at least one variant — orthogonal to the
cascade approach (which gates on confidence) and to plain HyDE (which is
single-variant).

Variants are 3 prompt framings, each with a different seed:
  1. SDK / API call style ("write a function that calls the X API")
  2. Identifier name style ("list the function/struct/module names")
  3. Crate path style ("which crate contains this functionality")

Run:
    OLLAMA_EMBED_MODEL=nomic-embed-text SKYGREP_DB_PATH=/tmp/<repo-D>_idx_p1.db \
      .venv/bin/python benchmarks/multi_hyde_probe.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WARP = Path(os.environ.get("SKYGREP_BENCH_RUST_REPO", "/path/to/Rust workspace"))
sys.path.insert(0, str(REPO))

os.environ.setdefault("OLLAMA_EMBED_MODEL", "nomic-embed-text")
os.environ.setdefault("SKYGREP_DB_PATH", "/tmp/<repo-D>_idx_p1.db")

import requests

from skylakegrep.src.config import get_config
from skylakegrep.src.embeddings import get_embedder
from skylakegrep.src.hybrid import lexical_candidate_paths
from skylakegrep.src.storage import search

TASKS = json.loads((REPO / "benchmarks/cross_repo/rust-workspace.json").read_text())


VARIANT_PROMPTS = [
    (
        "sdk-call",
        1,
        "Write a SHORT (5-15 lines) Rust code snippet that uses external SDK /"
        " API calls to answer the question. Use realistic crate names like"
        " ``reqwest``, ``tokio``, ``async_openai``, ``serde_json``. Output only"
        " the code, no explanation, no markdown fences.\n\n"
        "Question: {q}\n\nHypothetical SDK-style code:",
    ),
    (
        "ident-list",
        2,
        "List 8-12 likely Rust function / struct / module identifier names a"
        " developer would use to implement the answer to this question. Output"
        " only the identifiers separated by spaces, no prose, no explanation.\n\n"
        "Question: {q}\n\nIdentifiers:",
    ),
    (
        "crate-path",
        3,
        "Which crate paths under ``crates/<name>/src/...`` and ``app/src/...``"
        " in a Rust workspace would most likely contain the answer? List 3-5"
        " plausible relative paths, one per line, no prose.\n\n"
        "Question: {q}\n\nLikely paths:",
    ),
]


def variant_text(label: str, seed: int, template: str, query: str) -> str:
    """Generate one hypothetical doc variant via Ollama."""
    cfg = get_config()
    try:
        r = requests.post(
            f"{cfg['ollama_url'].rstrip('/')}/api/generate",
            json={
                "model": cfg["llm_model"],
                "prompt": template.format(q=query),
                "stream": False,
                "options": {"temperature": 0, "seed": seed, "num_predict": 256},
            },
            timeout=120,
        )
        r.raise_for_status()
        text = r.json().get("response", "").strip()
    except requests.RequestException:
        text = ""
    if not text:
        return query
    return f"{query}\n\n{text}"


def hit(expected: str, paths: list[str]) -> bool:
    return any(expected in p for p in paths)


def main() -> None:
    conn = sqlite3.connect("/tmp/<repo-D>_idx_p1.db")
    embedder = get_embedder(role="query")

    n = len(TASKS)
    print(f"multi-HyDE probe over {n} Rust workspace tasks\n")

    # Strategy: union top-K from each variant, dedup, return top-N.
    for label, fn in [
        (
            "single (sdk-call)",
            lambda q: [variant_text("sdk-call", 1, VARIANT_PROMPTS[0][2], q)],
        ),
        (
            "single (ident-list)",
            lambda q: [variant_text("ident-list", 2, VARIANT_PROMPTS[1][2], q)],
        ),
        (
            "union(3 variants)",
            lambda q: [
                variant_text(lbl, sd, tpl, q) for (lbl, sd, tpl) in VARIANT_PROMPTS
            ],
        ),
    ]:
        hits = 0
        total_t = 0.0
        misses = []
        for t in TASKS:
            q, exp = t["question"], t["expected"]
            cands = lexical_candidate_paths(q, WARP)
            t0 = time.perf_counter()
            variants = fn(q)
            seen = set()
            paths: list[str] = []
            for v in variants:
                emb = embedder.embed(v)
                res = search(
                    conn,
                    emb,
                    top_k=10,
                    query_text=q,
                    rerank=False,
                    multi_resolution=True,
                    file_top=30,
                    candidate_paths=cands,
                    rank_by="file",
                )
                for r in res:
                    p = r["path"]
                    if p not in seen:
                        seen.add(p)
                        paths.append(p)
            total_t += time.perf_counter() - t0
            if hit(exp, paths):
                hits += 1
            else:
                misses.append(exp)
        print(f"  {label:<22} : {hits}/{n}  total {total_t:.2f}s ({total_t/n:.2f}s/q)")
        if misses:
            print(f"    misses: {misses}")


if __name__ == "__main__":
    main()
