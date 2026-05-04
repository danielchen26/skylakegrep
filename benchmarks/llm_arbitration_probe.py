"""LLM filename arbitration probe.

Insight: a human searching with `rg` localizes mostly via filename judgment,
not chunk reading. The LLM is good at semantic filename-vs-question matching
even without seeing chunk bodies. Latency is small because the prompt is just
~20 path strings.

Strategies tested:
  - LFA-rerank: rg prefilter → top-N paths (by filename-cosine) → LLM picks
    top-K of those → cosine on K only → top file-rank.
  - LFA-only:   rg prefilter → all paths → LLM picks top-K → return paths
    directly without any cosine.

Run:
    OLLAMA_EMBED_MODEL=nomic-embed-text SKYGREP_DB_PATH=/tmp/<repo-D>_idx_p1.db \
      .venv/bin/python benchmarks/llm_arbitration_probe.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path("/Users/tianchichen/Documents/github/skylakegrep")
WARP = Path("/path/to/Rust workspace")
sys.path.insert(0, str(REPO))

os.environ.setdefault("OLLAMA_EMBED_MODEL", "nomic-embed-text")
os.environ.setdefault("SKYGREP_DB_PATH", "/tmp/<repo-D>_idx_p1.db")

import numpy as np
import requests

from skylakegrep.src.config import get_config
from skylakegrep.src.embeddings import get_embedder
from skylakegrep.src.hybrid import lexical_candidate_paths
from skylakegrep.src.storage import file_level_search, search

TASKS = json.loads((REPO / "benchmarks/cross_repo/rust-workspace.json").read_text())


def hit(expected: str, paths: list[str]) -> bool:
    return any(expected in p for p in paths)


def llm_pick(question: str, candidates: list[str], k: int) -> list[str]:
    """Ask qwen2.5:3b to pick the K most likely paths for the given question.

    Returns the LLM's chosen subset (intersected with the original list, in
    LLM-preferred order). Falls back to the original list when the call fails
    or the response is unparseable.
    """
    cfg = get_config()
    if not candidates:
        return []
    # Show paths as short relative paths, with a 1-indexed handle the LLM
    # can refer back to. Strip the workspace root prefix so the prompt is
    # compact.
    root_prefix = str(WARP) + "/"
    rels = [c[len(root_prefix):] if c.startswith(root_prefix) else c for c in candidates]
    listing = "\n".join(f"{i+1}. {p}" for i, p in enumerate(rels))
    prompt = (
        "You are helping localize code in a Rust workspace. Given a question "
        "and a list of candidate file paths, return the indices of the "
        f"{k} paths most likely to contain the answer. Output only a JSON "
        "array of integers (e.g. [3, 7, 12]). No prose.\n\n"
        f"Question: {question}\n\n"
        f"Paths:\n{listing}\n\n"
        "Indices:"
    )
    try:
        r = requests.post(
            f"{cfg['ollama_url'].rstrip('/')}/api/generate",
            json={
                "model": cfg["llm_model"],
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0, "seed": 42, "num_predict": 64},
            },
            timeout=60,
        )
        r.raise_for_status()
        text = r.json().get("response", "").strip()
    except requests.RequestException:
        return candidates[:k]
    # Find first JSON array in the response.
    m = re.search(r"\[[^\]]*\]", text)
    if not m:
        return candidates[:k]
    try:
        idxs = json.loads(m.group(0))
    except json.JSONDecodeError:
        return candidates[:k]
    out = []
    for i in idxs:
        try:
            j = int(i) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= j < len(candidates) and candidates[j] not in out:
            out.append(candidates[j])
        if len(out) >= k:
            break
    return out or candidates[:k]


def lfa_only(conn, embedder, q: str, cands: set[str], pool_n: int, pick_k: int) -> list[str]:
    """LFA-only: feed up to pool_n paths to the LLM, take top pick_k as result."""
    if not cands:
        return []
    qv = np.array(embedder.embed(q), dtype=np.float32)
    pool_paths = file_level_search(conn, qv, top_files=pool_n, candidate_paths=cands)
    if not pool_paths:
        pool_paths = sorted(cands)[:pool_n]
    return llm_pick(q, pool_paths, pick_k)


def lfa_rerank(
    conn, embedder, q: str, cands: set[str], pool_n: int, pick_k: int
) -> list[str]:
    """LFA-rerank: LLM picks K from pool, then chunk cosine restricted to those K."""
    if not cands:
        return []
    qv = np.array(embedder.embed(q), dtype=np.float32)
    pool_paths = file_level_search(conn, qv, top_files=pool_n, candidate_paths=cands)
    if not pool_paths:
        pool_paths = sorted(cands)[:pool_n]
    picked = llm_pick(q, pool_paths, pick_k)
    if not picked:
        return []
    res = search(
        conn,
        embedder.embed(q),
        top_k=10,
        query_text=q,
        rerank=False,
        multi_resolution=True,
        file_top=30,
        candidate_paths=set(picked),
        rank_by="file",
    )
    return [r["path"] for r in res]


def main() -> None:
    conn = sqlite3.connect("/tmp/<repo-D>_idx_p1.db")
    embedder = get_embedder(role="query")

    n = len(TASKS)
    print(f"LLM filename arbitration probe over {n} Rust workspace tasks\n")

    for label, fn in [
        ("LFA-only(20→5)", lambda q, c: lfa_only(conn, embedder, q, c, 20, 5)),
        ("LFA-only(30→5)", lambda q, c: lfa_only(conn, embedder, q, c, 30, 5)),
        ("LFA-rerank(20→5)", lambda q, c: lfa_rerank(conn, embedder, q, c, 20, 5)),
        ("LFA-rerank(30→5)", lambda q, c: lfa_rerank(conn, embedder, q, c, 30, 5)),
    ]:
        hits = 0
        total_t = 0.0
        misses = []
        for t in TASKS:
            q, exp = t["question"], t["expected"]
            t0 = time.perf_counter()
            cands = lexical_candidate_paths(q, WARP)
            paths = fn(q, cands)
            total_t += time.perf_counter() - t0
            if hit(exp, paths):
                hits += 1
            else:
                misses.append(exp)
        print(
            f"  {label:<20} : {hits}/{n}  total {total_t:.2f}s ({total_t/n:.2f}s/q)"
        )
        if misses:
            print(f"    misses: {misses}")


if __name__ == "__main__":
    main()
