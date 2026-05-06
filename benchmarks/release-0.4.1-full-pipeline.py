"""Full-pipeline real-corpus bench for 0.4.1 — runs the actual skygrep
CLI search command on a properly indexed corpus, exercising EVERY layer:

  1. LLM router (qwen2.5:3b → RouterDecision: intent / scope / primary_token)
  2. Filename shortcut for filename intent (auto_index)
  3. Ripgrep prefilter for lexical intent
  4. cascade_search (cosine + σ-adaptive escalation)
  5. Symbol-aware boost (tree-sitter symbols populated at index time)
  6. Multi-channel RRF fuse
  7. Graph-prior tiebreak (file_graph PageRank)
  8. 0.4.0 graph_expand (1-hop reference-graph neighbour expansion)

This replaces the 0.4.1 sub-component bench (cascade_search direct +
empty symbols table) which was an unfair under-estimate of the system.

Per the auto-memory rule
``feedback_full_intelligent_routing_pipeline.md``: bench the FULL
pipeline via the CLI, never one piece in isolation.

Usage:
    .venv/bin/python benchmarks/release-0.4.1-full-pipeline.py
"""

from __future__ import annotations
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
SKYGREP = REPO_ROOT / ".venv" / "bin" / "python"

# 5 representative semantic queries with ground-truth expected file
QUERIES = [
    ("how does the cascade decide whether to escalate to HyDE",   "storage.py"),
    ("how does proactive enhancement work after a low-confidence result", "proactive.py"),
    ("how is the LLM router decision cached",                     "llm_router.py"),
    ("how does the v2 graph expansion add candidates",            "storage.py"),
    ("how does symbol-aware ranking boost results",               "storage.py"),
]


def run_search(query: str, top_k: int = 5) -> tuple[list[str], str]:
    """Invoke skygrep search via the installed CLI; return (top_paths, telemetry_text).

    Excludes ``benchmarks/`` since the bench scripts themselves contain the
    query strings verbatim → they'd crowd out the real answer files.
    """
    cmd = [
        str(SKYGREP), "-m", "skylakegrep.src.cli",
        "search", query, "--top", str(top_k),
        "--exclude", "benchmarks/*",
        "--exclude", "*release-*",
    ]
    res = subprocess.run(
        cmd, capture_output=True, text=True, timeout=180,
        cwd=str(REPO_ROOT),
        env={**os.environ, "OLLAMA_URL": "http://localhost:11434"},
    )
    out = res.stdout + res.stderr
    # Result-card opening line: "╭─ <path>:<start>-<end> …"
    paths: list[str] = []
    for m in re.finditer(r"╭─\s*([^\s:]+):\d+", out):
        path = m.group(1)
        if path not in paths:
            paths.append(path)
    return paths, out


def parse_telemetry(out: str) -> dict:
    """Parse the telemetry footer block. Returns {} if no footer present."""
    info: dict = {}
    # The footer starts after ✓ 0.XXX s · quality=BEST and has indented lines:
    #     path     : cosine-cheap (high-confidence early-exit)
    #     router   : llm → intent=semantic (0.83)
    #     evidence : σ-gap=0.0820 ≥ τ=0.0050 (adaptive)
    # We look for the path line that has at least one space before "path"
    # AND is followed within ~5 lines by "router" or "evidence" — that
    # anchors us to the real telemetry block, not random "path = ..."
    # imports inside snippets.
    block_match = re.search(
        r"path\s*:\s*([\w-]+)[^\n]*\n[^\n]*router\s*:\s*([^\n]+)\n[^\n]*evidence",
        out
    )
    if block_match:
        info["path"] = block_match.group(1)
        info["router"] = block_match.group(2).strip()
    return info


def main() -> int:
    print(f"{'='*82}")
    print(f"skylakegrep 0.4.1 FULL-PIPELINE bench — via skygrep CLI on the real index")
    print(f"  (LLM router + filename shortcut + cascade + symbol boost +")
    print(f"   graph tiebreak + 0.4.0 graph_expand)")
    print(f"{'='*82}\n")

    fired_paths = {"cosine-cheap": 0, "cosine-escalated-rerank": 0, "graph-walk": 0,
                   "filename-shortcut": 0, "rg-only": 0, "other": 0}
    hits = 0
    n = len(QUERIES)
    for i, (q, expect) in enumerate(QUERIES, 1):
        t0 = time.perf_counter()
        paths, out = run_search(q)
        elapsed = time.perf_counter() - t0
        info = parse_telemetry(out)
        path = info.get("path") or "(no telemetry block found)"
        fired_paths[path] = fired_paths.get(path, 0) + 1

        top_basenames = [Path(p).name for p in paths[:5]]
        hit = any(expect.lower() in p.lower() for p in top_basenames)
        if hit: hits += 1

        gex_match = re.search(r"graph_expand", out)
        gex = "✓" if gex_match else "·"

        print(f"Q{i}. {q!r}")
        print(f"     elapsed: {elapsed:5.2f}s   path: {path}   graph_expand: {gex}")
        print(f"     top-5: {top_basenames}")
        print(f"     expect '{expect}': {'✓ HIT' if hit else '✗ miss'}")
        print()

    print(f"{'='*82}")
    print(f"SUMMARY: {hits}/{n} hit ({100*hits//n}%)")
    print(f"  routing path distribution: {dict((k,v) for k,v in fired_paths.items() if v)}")
    print(f"{'='*82}")
    return 0 if hits >= 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
