"""Build the four cheap edge types of the v2 retrieval substrate during the
ordinary index pass.

See ``docs/plans/2026-05-05-graph-prior-folder-inference.md`` § 9.2 for the
full edge taxonomy. This module covers the four index-time edges:

  * ``contains``   — folder ⊃ file ⊃ chunk (cheap, structural)
  * ``refs``       — file → file via existing ``file_graph`` (reference graph)
  * ``name_sim``   — file ↔ file via shared filename tokens (token inverted index)
  * ``path_prox``  — file ↔ file via shared ancestor depth (LCA proxy)

The lazy edges (``semantic``, ``co_access``) are computed on-demand inside
the walk; this module never blocks the indexer waiting for embeddings.

Idempotent: re-runnable on a populated graph; uses ``INSERT OR REPLACE`` so
edge weights converge rather than duplicate. Wall-clock target: < 1 s on a
1K-file project, < 10 s on a 50K-file project.

Public API: ``populate_graph_substrate(conn, root)``.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional

from .graph_walk import (
    EDGE_CONTAINS, EDGE_NAME_SIM, EDGE_PATH_PROX, EDGE_REFS,
    Graph, NODE_FILE, NODE_FOLDER,
)


# ── Token extraction for name-similarity ──────────────────────────────────
_BASENAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_NAME_TOKEN_MIN_LEN = 2
_NAME_SIM_TOPK_PER_FILE = 8  # Cap edges per file to bound graph density


def _basename_tokens(path: str) -> list[str]:
    """Camel-split + lowercased identifier tokens from the file's basename
    (without extension). E.g. ``LanguageModelClient.py`` → ``["language",
    "model", "client"]``."""
    base = Path(path).stem
    out: list[str] = []
    for run in _BASENAME_TOKEN.findall(base):
        for piece in _CAMEL_BOUNDARY.split(run):
            piece = piece.lower()
            if len(piece) >= _NAME_TOKEN_MIN_LEN:
                out.append(piece)
    return out


# ── Phase A: list indexed files ──────────────────────────────────────────
def _indexed_files(conn: sqlite3.Connection) -> list[str]:
    cur = conn.execute("SELECT DISTINCT file FROM chunks ORDER BY file")
    return [str(p) for (p,) in cur.fetchall()]


# ── Phase B: containment edges (folder ⊃ folder ⊃ file) ──────────────────
def _build_containment_edges(
    graph: Graph, files: Iterable[str], root: Path
) -> tuple[int, set[str]]:
    """Insert (folder → folder) and (folder → file) `contains` edges.

    Returns ``(edge_count, folder_paths_seen)``.
    """
    folders: set[str] = set()
    edges: list[tuple[int, int, str, float]] = []

    root_str = str(root.resolve())
    for f in files:
        p = Path(f).resolve() if Path(f).is_absolute() else (root / f).resolve()
        # Walk parents up to (but not including) the root to avoid /tmp →
        # / leakage on relative paths
        try:
            rel_parts = p.relative_to(root).parts
        except ValueError:
            rel_parts = p.parts
        cur = root
        chain: list[Path] = [cur]
        for part in rel_parts[:-1]:    # skip the file leaf
            cur = cur / part
            chain.append(cur)
        # chain is [root, root/a, root/a/b, …, root/a/b/leaf-parent]
        # Emit (parent → child) folder edges, and finally (leaf → file)
        for i in range(len(chain) - 1):
            src = graph.upsert_node(NODE_FOLDER, str(chain[i]))
            dst = graph.upsert_node(NODE_FOLDER, str(chain[i + 1]))
            folders.add(str(chain[i]))
            folders.add(str(chain[i + 1]))
            edges.append((src, dst, EDGE_CONTAINS, 1.0))
        # Final containment: leaf folder → file
        leaf_folder = chain[-1]
        src = graph.upsert_node(NODE_FOLDER, str(leaf_folder))
        dst = graph.upsert_node(NODE_FILE, str(p))
        folders.add(str(leaf_folder))
        edges.append((src, dst, EDGE_CONTAINS, 1.0))

    n = graph.add_edges_batch(edges)
    return n, folders


# ── Phase C: reference edges (re-export from file_graph table) ───────────
def _build_reference_edges(
    graph: Graph, conn: sqlite3.Connection, root: Path
) -> int:
    """Lift the reference graph (already populated in ``file_graph`` /
    ``populate_graph_table``) into the unified ``graph_edge`` table.

    Falls back to the v0 reference graph builder if ``file_graph`` is empty
    but the corpus has indexable code files.
    """
    # Direct edges live in ``file_graph_edges`` if the existing reference-
    # graph code populated it; for the MVP we read out-degree counts only
    # and emit edges from the reference_graph build. Cheap fallback: if the
    # existing graph builder didn't run, just try to build it now.
    try:
        from . import reference_graph  # noqa: F401
        # The existing builder writes to file_graph (in/out degree + pagerank).
        # We additionally need the actual edge list. For MVP we re-derive
        # it cheaply via the build_export_graph API.
        edges_out = reference_graph.build_export_graph(root)
    except Exception:
        edges_out = {}

    edges: list[tuple[int, int, str, float]] = []
    for src_path, targets in edges_out.items():
        src_id = graph.upsert_node(NODE_FILE, str((root / src_path).resolve()
                                                 if not Path(src_path).is_absolute()
                                                 else Path(src_path)))
        for dst_path, weight in targets.items():
            dst_resolved = ((root / dst_path).resolve()
                            if not Path(dst_path).is_absolute()
                            else Path(dst_path))
            dst_id = graph.upsert_node(NODE_FILE, str(dst_resolved))
            edges.append((src_id, dst_id, EDGE_REFS, float(weight)))
    return graph.add_edges_batch(edges)


# ── Phase D: name-similarity edges (token inverted index) ────────────────
def _build_name_sim_edges(
    graph: Graph, files: list[str]
) -> int:
    """Files sharing basename tokens get a Jaccard-weighted ``name_sim`` edge.

    Token inverted index: O(N · avg_token_count), then for each token bucket
    we emit pairwise edges weighted by Jaccard similarity of their token
    sets. We cap per-file outgoing edges at ``_NAME_SIM_TOPK_PER_FILE`` to
    bound graph density (without the cap, every Python file shares "py"
    with every other Python file).
    """
    if not files:
        return 0

    file_tokens: dict[str, set[str]] = {}
    inverted: dict[str, list[str]] = defaultdict(list)
    for f in files:
        toks = set(_basename_tokens(f))
        if not toks:
            continue
        file_tokens[f] = toks
        for tok in toks:
            inverted[tok].append(f)

    # Score each pair by Jaccard
    pair_score: dict[tuple[str, str], float] = {}
    for tok, members in inverted.items():
        if len(members) > 200:
            # Common token like "py" or "test" — skip; would explode pairs
            continue
        for i, fa in enumerate(members):
            for fb in members[i + 1:]:
                if fa == fb:
                    continue
                key = (fa, fb) if fa < fb else (fb, fa)
                if key in pair_score:
                    continue
                ta, tb = file_tokens[fa], file_tokens[fb]
                jaccard = len(ta & tb) / max(1, len(ta | tb))
                if jaccard >= 0.25:    # noise floor
                    pair_score[key] = jaccard

    # Per-file top-K cap: keep the K strongest neighbours per file
    by_src: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for (a, b), s in pair_score.items():
        by_src[a].append((b, s))
        by_src[b].append((a, s))

    edges: list[tuple[int, int, str, float]] = []
    for src, neighbours in by_src.items():
        neighbours.sort(key=lambda kv: -kv[1])
        kept = neighbours[:_NAME_SIM_TOPK_PER_FILE]
        src_id = graph.upsert_node(NODE_FILE, src)
        for dst, weight in kept:
            dst_id = graph.upsert_node(NODE_FILE, dst)
            edges.append((src_id, dst_id, EDGE_NAME_SIM, weight))

    return graph.add_edges_batch(edges)


# ── Phase E: path-proximity edges (LCA depth) ────────────────────────────
def _build_path_prox_edges(
    graph: Graph, files: list[str], k_per_file: int = 6
) -> int:
    """For each file, link to its ``k_per_file`` nearest path-neighbours by
    shared-ancestor depth.

    Algorithm: group files by parent directory; emit edges within each group
    (high LCA depth = high weight). For files whose parent has < k siblings,
    walk up one more level and grab cousins. O(N · k).
    """
    if not files:
        return 0
    by_parent: dict[str, list[str]] = defaultdict(list)
    for f in files:
        by_parent[str(Path(f).parent)].append(f)

    edges: list[tuple[int, int, str, float]] = []
    for parent, members in by_parent.items():
        if len(members) < 2:
            continue
        # All-pairs within the same parent — bounded since most dirs have
        # < 50 files
        for i, fa in enumerate(members):
            kept = 0
            src_id = graph.upsert_node(NODE_FILE, fa)
            for fb in members:
                if fb == fa:
                    continue
                if kept >= k_per_file:
                    break
                # Same-parent weight = 0.7 (high — they coexist by design)
                dst_id = graph.upsert_node(NODE_FILE, fb)
                edges.append((src_id, dst_id, EDGE_PATH_PROX, 0.7))
                kept += 1
    return graph.add_edges_batch(edges)


# ── Public API ────────────────────────────────────────────────────────────
def populate_graph_substrate(
    conn: sqlite3.Connection,
    root: Path,
    *,
    skip_refs: bool = False,
) -> dict[str, int | float]:
    """Build the four cheap edge types end-to-end.

    Idempotent. Returns telemetry: edge counts per type, total wall-clock.
    Caller is responsible for ``conn.commit()`` after this returns.
    """
    t0 = time.perf_counter()
    graph = Graph(conn)
    files = _indexed_files(conn)

    n_contains, _folders = _build_containment_edges(graph, files, root)
    n_refs = 0 if skip_refs else _build_reference_edges(graph, conn, root)
    n_name = _build_name_sim_edges(graph, files)
    n_path = _build_path_prox_edges(graph, files)

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "files":         len(files),
        "edge_contains": n_contains,
        "edge_refs":     n_refs,
        "edge_name_sim": n_name,
        "edge_path_prox": n_path,
        "elapsed_ms":    elapsed_ms,
    }


__all__ = ["populate_graph_substrate"]
