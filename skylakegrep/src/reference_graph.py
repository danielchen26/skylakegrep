# SPDX-License-Identifier: Apache-2.0
"""Content-agnostic reference-graph builder.

The legacy ``code_graph`` module hard-coded Rust / Python / JS / TS regex
extractors and walked the source tree once. This module replaces that with a
pluggable registry so future content types — markdown, YAML / TOML configs,
RDF / knowledge graphs — can drop in without touching retrieval code.

A reference extractor is any callable with the signature
``(files: list[Path], root: Path) -> list[tuple[str, str]]`` returning
``(src_path, dst_path)`` edges. The ``REFERENCE_EXTRACTORS`` registry maps
content-type tags (currently file-suffix sets, e.g. ``"code"`` or
``"markdown"``) to such callables.

Adding a new extractor is three lines:

    from .extractors import config as config_extractor
    REFERENCE_EXTRACTORS["config"] = config_extractor.extract_edges
    CONTENT_TYPE_EXTENSIONS["config"] = {".yaml", ".yml", ".toml"}

Files are dispatched to the first extractor whose extension set contains
their suffix; everything unmatched is silently skipped — matching the
legacy behaviour. The output shape (``{file: {in_degree, out_degree,
pagerank}}``) is preserved so ``storage`` / ``cli`` consumers don't change.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from .extractors import code as code_extractor
from .extractors import markdown as markdown_extractor

# ---------------------------------------------------------------------------
# Walk policy.
# ---------------------------------------------------------------------------

_IGNORED_DIRS = {
    ".git", ".hg", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".venv", "__pycache__", "build", "dist", "node_modules", "target",
    "vendor",
}


# ---------------------------------------------------------------------------
# Registry.
# ---------------------------------------------------------------------------

#: Per-content-type extension sets used for dispatch.
CONTENT_TYPE_EXTENSIONS: dict[str, set[str]] = {
    "code": code_extractor.ALL_EXT,
    "markdown": markdown_extractor.MARKDOWN_EXT,
}

#: ``content_type → extract_edges`` callable. The order of insertion is the
#: dispatch priority — ``"code"`` is consulted first (preserves legacy
#: behaviour for repos that mix ``.md`` files and code).
ExtractorFn = Callable[[list[Path], Path], list[tuple[str, str]]]
REFERENCE_EXTRACTORS: dict[str, ExtractorFn] = {
    "code": code_extractor.extract_edges,
    "markdown": markdown_extractor.extract_edges,
}


def register_extractor(
    content_type: str,
    extensions: set[str],
    fn: ExtractorFn,
) -> None:
    """Register a new content type without touching this module's source.

    Intended for plugin authors / tests; the in-tree extractors register
    themselves at import time via the constants above.
    """

    CONTENT_TYPE_EXTENSIONS[content_type] = set(extensions)
    REFERENCE_EXTRACTORS[content_type] = fn


# ---------------------------------------------------------------------------
# File walk + dispatch.
# ---------------------------------------------------------------------------


def _is_ignored(path: Path) -> bool:
    parts = set(path.parts)
    return bool(parts & _IGNORED_DIRS)


def _all_known_extensions() -> set[str]:
    out: set[str] = set()
    for exts in CONTENT_TYPE_EXTENSIONS.values():
        out |= exts
    return out


def _classify(path: Path) -> str | None:
    """Return the content-type tag for ``path`` or ``None`` if unknown."""
    suf = path.suffix
    for content_type, exts in CONTENT_TYPE_EXTENSIONS.items():
        if suf in exts:
            return content_type
    return None


def _collect_files(root: Path) -> dict[str, list[Path]]:
    """Walk ``root`` once, partitioning files by content-type tag."""
    buckets: dict[str, list[Path]] = defaultdict(list)
    known = _all_known_extensions()
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix not in known:
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if _is_ignored(rel):
            continue
        ct = _classify(p)
        if ct is None:
            continue
        buckets[ct].append(p)
    return buckets


# ---------------------------------------------------------------------------
# PageRank (sparse, no NumPy adjacency matrix).
# ---------------------------------------------------------------------------


def _pagerank(
    nodes: list[str],
    inbound: dict[str, list[str]],
    out_degree: dict[str, int],
    *,
    damping: float = 0.85,
    iterations: int = 50,
) -> dict[str, float]:
    n = len(nodes)
    if n == 0:
        return {}
    pr = {v: 1.0 / n for v in nodes}
    teleport = (1.0 - damping) / n
    for _ in range(iterations):
        # Dangling-node mass: PageRank from nodes with out_degree 0 is
        # spread uniformly so total mass is preserved.
        dangling = sum(pr[v] for v in nodes if out_degree.get(v, 0) == 0)
        dangling_share = damping * dangling / n
        new = {}
        for v in nodes:
            inflow = 0.0
            for u in inbound.get(v, ()):
                deg = out_degree.get(u, 0)
                if deg > 0:
                    inflow += pr[u] / deg
            new[v] = teleport + dangling_share + damping * inflow
        pr = new
    return pr


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def build_export_graph(root: Path) -> dict[str, dict[str, float]]:
    """Walk all known files under ``root`` and return per-file graph stats.

    Returned shape: ``{file_path: {"in_degree": int, "out_degree": int,
    "pagerank": float}}``. Identical to the legacy ``code_graph`` shape — the
    new content-type plugins only enlarge the candidate node set.
    """

    root = Path(root).resolve()
    buckets = _collect_files(root)
    edges: list[tuple[str, str]] = []
    all_files: list[Path] = []
    for content_type, files in buckets.items():
        if not files:
            continue
        all_files.extend(files)
        extractor = REFERENCE_EXTRACTORS.get(content_type)
        if extractor is None:
            continue
        edges.extend(extractor(files, root))

    nodes = sorted({str(f) for f in all_files})
    in_degree: dict[str, int] = defaultdict(int)
    out_degree: dict[str, int] = defaultdict(int)
    inbound: dict[str, list[str]] = defaultdict(list)
    for src, dst in edges:
        in_degree[dst] += 1
        out_degree[src] += 1
        inbound[dst].append(src)

    pr = _pagerank(nodes, inbound, out_degree)

    out: dict[str, dict[str, float]] = {}
    for v in nodes:
        out[v] = {
            "in_degree": int(in_degree.get(v, 0)),
            "out_degree": int(out_degree.get(v, 0)),
            "pagerank": float(pr.get(v, 0.0)),
        }
    return out


def populate_graph_table(conn, root: Path) -> int:
    """Build the export graph for ``root`` and write to ``file_graph``
    (per-file PageRank stats) plus ``graph_edge`` (the actual reference
    edges, used by 0.4.0+ holistic cascade neighbour expansion).

    Edge weight = PageRank of the destination node — data-derived, no
    preset. Cascade scoring uses cosine; PageRank only orders the SQL
    fetch when we ask for top-K neighbours.

    Returns the number of nodes processed. Idempotent.
    """

    root = Path(root).resolve()
    # We need both the aggregated stats (for file_graph) and the raw
    # edge list (for graph_edge). Recompute the inputs once and produce
    # both — cheaper than calling build_export_graph and a separate
    # edge collector.
    buckets = _collect_files(root)
    edges_raw: list[tuple[str, str]] = []
    all_files: list[Path] = []
    for content_type, files in buckets.items():
        if not files:
            continue
        all_files.extend(files)
        extractor = REFERENCE_EXTRACTORS.get(content_type)
        if extractor is None:
            continue
        edges_raw.extend(extractor(files, root))

    nodes = sorted({str(f) for f in all_files})
    in_degree: dict[str, int] = defaultdict(int)
    out_degree: dict[str, int] = defaultdict(int)
    inbound: dict[str, list[str]] = defaultdict(list)
    for src, dst in edges_raw:
        in_degree[dst] += 1
        out_degree[src] += 1
        inbound[dst].append(src)
    pr = _pagerank(nodes, inbound, out_degree)

    # ── file_graph (per-file aggregate) ─────────────────────────────
    conn.execute("DELETE FROM file_graph")
    rows = []
    for v in nodes:
        try:
            mtime = Path(v).stat().st_mtime
        except OSError:
            mtime = 0.0
        rows.append((v, int(in_degree.get(v, 0)), int(out_degree.get(v, 0)),
                     float(pr.get(v, 0.0)), mtime))
    conn.executemany(
        "INSERT INTO file_graph (file, in_degree, out_degree, pagerank, file_mtime) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )

    # 0.5.0: graph_edge[refs] is now populated LAZILY by
    # ``lazy_indexer.ensure_refs_for(files)`` only on files the cascade
    # actually reaches — never as an upfront global pass. The 0.4.x
    # eager population was removed as dead code. The schema (graph_node
    # / graph_edge tables) is preserved and used by the lazy populator.
    conn.commit()
    return len(nodes)
    return len(rows)
