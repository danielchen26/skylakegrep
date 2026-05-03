"""Code-graph centrality probe.

Insight: when multiple semantically similar files compete (`crates/websocket/
lib.rs` vs `crates/websocket/tests/integration.rs` vs `crates/network/
websocket.rs`), the canonical answer is usually the one *imported by the most
other files*. Module in-degree is a structural signal orthogonal to cosine
that should help disambiguation.

Phase 1 (offline): scan all .rs files under WARP for `use crate::...` and `mod
...` references; resolve target file via Cargo workspace conventions; build a
file→in-degree map.

Phase 2 (online): run Round B; multiply each candidate's cosine score by
``1 + alpha * log(1 + in_degree)``; sort and return.

Run:
    OLLAMA_EMBED_MODEL=nomic-embed-text MGREP_DB_PATH=/tmp/<repo-D>_idx_p1.db \
      .venv/bin/python benchmarks/code_graph_probe.py
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

REPO = Path("/Users/tianchichen/Documents/github/local-mgrep")
WARP = Path("/Users/tianchichen/Documents/github/<repo-D>")
sys.path.insert(0, str(REPO))

os.environ.setdefault("OLLAMA_EMBED_MODEL", "nomic-embed-text")
os.environ.setdefault("MGREP_DB_PATH", "/tmp/<repo-D>_idx_p1.db")

import numpy as np

from local_mgrep.src.embeddings import get_embedder
from local_mgrep.src.hybrid import lexical_candidate_paths

TASKS = json.loads((REPO / "benchmarks/cross_repo/<repo-D>.json").read_text())


_USE_CRATE_RE = re.compile(r"\buse\s+crate::([A-Za-z0-9_:]+)")
_USE_SELF_RE = re.compile(r"\buse\s+(?:super|self)::([A-Za-z0-9_:]+)")
_USE_EXTERN_RE = re.compile(r"\buse\s+([a-z][a-z0-9_]+)::([A-Za-z0-9_:]+)")
_MOD_RE = re.compile(r"^\s*(?:pub\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", re.MULTILINE)


def _path_to_module(path: Path, root: Path) -> tuple[str | None, str | None]:
    """Return (crate_name, module_path) for a Rust source file, or (None, None).

    A file ``<repo-D>/crates/foo/src/bar/baz.rs`` resolves to crate=foo,
    module=bar::baz. ``mod.rs`` and ``lib.rs`` collapse to the parent module.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None, None
    parts = rel.parts
    # Look for crates/<crate>/src/...
    if "crates" in parts:
        i = parts.index("crates")
        if i + 2 < len(parts) and parts[i + 2] == "src":
            crate = parts[i + 1].replace("-", "_")
            mod_parts = list(parts[i + 3 :])
            if not mod_parts:
                return crate, ""
            last = mod_parts[-1]
            if last in ("lib.rs", "main.rs", "mod.rs"):
                mod_parts = mod_parts[:-1]
            else:
                mod_parts[-1] = last.removesuffix(".rs")
            return crate, "::".join(mod_parts)
    # app/src/<...>.rs (<repo-D>'s binary crate-ish layout)
    if len(parts) >= 2 and parts[0] == "app" and parts[1] == "src":
        mod_parts = list(parts[2:])
        if not mod_parts:
            return "app", ""
        last = mod_parts[-1]
        if last in ("lib.rs", "main.rs", "mod.rs"):
            mod_parts = mod_parts[:-1]
        else:
            mod_parts[-1] = last.removesuffix(".rs")
        return "app", "::".join(mod_parts)
    return None, None


def build_indegree(root: Path) -> dict[str, int]:
    """Walk all .rs files; count crate-level in-references per file."""
    files: list[Path] = []
    for p in root.rglob("*.rs"):
        # Skip target/ build artifacts.
        if "/target/" in str(p):
            continue
        files.append(p)
    # Build crate::module → file map.
    mod_to_file: dict[tuple[str, str], str] = {}
    for f in files:
        crate, mod = _path_to_module(f, root)
        if crate is None:
            continue
        mod_to_file[(crate, mod)] = str(f)
    indeg: dict[str, int] = defaultdict(int)
    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        crate, _ = _path_to_module(f, root)
        # use crate::a::b → reference (this_crate, "a::b")
        for m in _USE_CRATE_RE.finditer(text):
            mod_path = m.group(1)
            target = (crate or "", _trim_last_segment(mod_path))
            target_file = _resolve(mod_to_file, target)
            if target_file and target_file != str(f):
                indeg[target_file] += 1
        # use foo::a::b → external crate "foo"
        for m in _USE_EXTERN_RE.finditer(text):
            ext_crate = m.group(1)
            mod_path = m.group(2)
            if ext_crate in {"std", "core", "alloc", "tokio", "serde", "anyhow"}:
                continue
            target = (ext_crate, _trim_last_segment(mod_path))
            target_file = _resolve(mod_to_file, target)
            if target_file and target_file != str(f):
                indeg[target_file] += 1
        # mod foo;  → reference within same file's module to a sibling file
        for m in _MOD_RE.finditer(text):
            sub = m.group(1)
            crate_local, mod_local = _path_to_module(f, root) or ("", "")
            target_mod = f"{mod_local}::{sub}".lstrip(":") if mod_local else sub
            target = (crate_local or "", target_mod)
            target_file = _resolve(mod_to_file, target)
            if target_file and target_file != str(f):
                indeg[target_file] += 1
    return dict(indeg)


def _trim_last_segment(mod_path: str) -> str:
    """`a::b::SomeStruct` → `a::b` heuristically (treat last segment as item)."""
    parts = mod_path.split("::")
    if len(parts) <= 1:
        return mod_path
    # If last segment looks like an item (Pascal/all-caps or starts with lowercase
    # that's typically a function/const), drop it.
    return "::".join(parts[:-1])


def _resolve(mod_to_file: dict[tuple[str, str], str], target: tuple[str, str]) -> str | None:
    """Try exact, then prefix-shortened module paths."""
    crate, mod = target
    while True:
        if (crate, mod) in mod_to_file:
            return mod_to_file[(crate, mod)]
        if not mod:
            return None
        if "::" not in mod:
            mod = ""
        else:
            mod = mod.rsplit("::", 1)[0]


def hit(expected: str, paths: list[str]) -> bool:
    return any(expected in p for p in paths)


def round_b_with_scores(
    conn, qv: np.ndarray, candidate_paths: set[str], top_files: int = 10
) -> list[tuple[str, float]]:
    if not candidate_paths:
        return []
    placeholders = ",".join("?" * len(candidate_paths))
    rows = conn.execute(
        f"SELECT file, embedding FROM files WHERE file IN ({placeholders})",
        sorted(candidate_paths),
    ).fetchall()
    if not rows:
        return []
    matrix = np.vstack([np.frombuffer(blob, dtype=np.float32) for _, blob in rows])
    if matrix.shape[1] != qv.shape[0]:
        return []
    qn = float(np.linalg.norm(qv))
    denom = np.linalg.norm(matrix, axis=1) * qn + 1e-8
    scores = matrix @ qv / denom
    pairs = list(zip([r[0] for r in rows], [float(s) for s in scores]))
    pairs.sort(key=lambda kv: -kv[1])
    return pairs[:top_files]


def main() -> None:
    print("Building Rust import graph …", flush=True)
    t0 = time.perf_counter()
    indeg = build_indegree(WARP)
    t_idx = time.perf_counter() - t0
    print(f"  scanned <repo-D> in {t_idx:.2f}s; {len(indeg)} files have non-zero in-degree")
    if indeg:
        top = sorted(indeg.items(), key=lambda kv: -kv[1])[:8]
        for path, deg in top:
            rel = path.replace(str(WARP) + "/", "")
            print(f"    {deg:>4}  {rel}")
    print()

    conn = sqlite3.connect("/tmp/<repo-D>_idx_p1.db")
    embedder = get_embedder(role="query")

    n = len(TASKS)
    print(f"{'alpha':>6}  {'recall':>7}  {'total_s':>8}  {'avg_s/q':>8}")
    for alpha in [0.0, 0.05, 0.1, 0.15, 0.2, 0.3]:
        hits = 0
        total_t = 0.0
        misses = []
        for t in TASKS:
            q, exp = t["question"], t["expected"]
            t0 = time.perf_counter()
            cands = lexical_candidate_paths(q, WARP)
            qv = np.array(embedder.embed(q), dtype=np.float32)
            b = round_b_with_scores(conn, qv, cands, top_files=50)
            # Apply graph prior.
            scored = [
                (p, s * (1.0 + alpha * math.log(1.0 + indeg.get(p, 0))))
                for p, s in b
            ]
            scored.sort(key=lambda kv: -kv[1])
            paths = [p for p, _ in scored[:10]]
            total_t += time.perf_counter() - t0
            if hit(exp, paths):
                hits += 1
            else:
                misses.append(exp)
        print(f"{alpha:>6.2f}  {hits:>3}/{n:>3}  {total_t:>7.2f}s  {total_t/n:>7.2f}s")
        if misses:
            print(f"        misses: {misses}")


if __name__ == "__main__":
    main()
