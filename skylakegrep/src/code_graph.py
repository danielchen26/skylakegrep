"""File-export graph builder for the L4 PageRank tiebreaker.

Walks the source tree, regex-parses use/import edges across Rust, Python,
TypeScript and JavaScript, and persists ``(file, in_degree, out_degree,
pagerank)`` into the ``file_graph`` SQLite table.

The tiebreaker is consumed by ``storage.search`` and only fires when the
final top-1 / top-2 candidate scores are within ``TIEBREAK_EPS``. PageRank
is *not* used as a global cosine prior — that approach (P4-CGC) failed
because hub files like ``lib.rs`` overpowered canonical leaves. See
``docs/plans/2026-05-03-intelligent-system-v0.5.md`` §4.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Languages we parse. Anything else is silently skipped.
# ---------------------------------------------------------------------------

_RUST_EXT = {".rs"}
_PY_EXT = {".py"}
_TS_JS_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

_IGNORED_DIRS = {
    ".git", ".hg", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".venv", "__pycache__", "build", "dist", "node_modules", "target",
    "vendor",
}

# ---------------------------------------------------------------------------
# Rust regexes (mirrored from benchmarks/code_graph_probe.py).
# ---------------------------------------------------------------------------

_RUST_USE_CRATE_RE = re.compile(r"\buse\s+crate::([A-Za-z0-9_:]+)")
_RUST_USE_SELF_RE = re.compile(r"\buse\s+(?:super|self)::([A-Za-z0-9_:]+)")
_RUST_USE_EXTERN_RE = re.compile(r"\buse\s+([a-z][a-z0-9_]+)::([A-Za-z0-9_:]+)")
_RUST_MOD_RE = re.compile(
    r"^\s*(?:pub\s+)?mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;", re.MULTILINE
)
_RUST_EXTERN_SKIP = {
    "std", "core", "alloc", "tokio", "serde", "anyhow", "log", "tracing",
    "thiserror", "futures", "bytes", "regex", "chrono",
}

# ---------------------------------------------------------------------------
# Python regexes.
# ---------------------------------------------------------------------------

_PY_FROM_RE = re.compile(
    r"^\s*from\s+(\.+)?([A-Za-z_][A-Za-z0-9_.]*)\s+import\s+",
    re.MULTILINE,
)
_PY_FROM_REL_RE = re.compile(
    r"^\s*from\s+(\.+)\s+import\s+", re.MULTILINE
)
_PY_IMPORT_RE = re.compile(
    r"^\s*import\s+([A-Za-z_][A-Za-z0-9_.]*)", re.MULTILINE
)

# ---------------------------------------------------------------------------
# TS/JS regexes.
# ---------------------------------------------------------------------------

_TS_IMPORT_FROM_RE = re.compile(
    r"""\bimport\s+(?:[^'"`]+?\s+from\s+)?['"]([^'"]+)['"]""",
)
_TS_IMPORT_DYN_RE = re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)""")
_TS_REQUIRE_RE = re.compile(r"""\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)""")


# ---------------------------------------------------------------------------
# Path collection.
# ---------------------------------------------------------------------------


def _is_ignored(path: Path) -> bool:
    parts = set(path.parts)
    return bool(parts & _IGNORED_DIRS)


def _collect_files(root: Path) -> list[Path]:
    """All Rust / Python / TS / JS source files under ``root``.

    Skips the conventional ignored directories. We don't reuse
    ``indexer.collect_indexable_files`` because we want only the four
    languages whose import graph we actually parse.
    """

    exts = _RUST_EXT | _PY_EXT | _TS_JS_EXT
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.suffix not in exts:
            continue
        if _is_ignored(p.relative_to(root)):
            continue
        files.append(p)
    return files


# ---------------------------------------------------------------------------
# Rust module path resolution (same logic as the abandoned probe).
# ---------------------------------------------------------------------------


def _rust_path_to_module(
    path: Path, root: Path
) -> tuple[str | None, str | None]:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None, None
    parts = rel.parts
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


def _trim_last_segment(mod_path: str) -> str:
    parts = mod_path.split("::")
    if len(parts) <= 1:
        return mod_path
    return "::".join(parts[:-1])


def _resolve_rust(
    mod_to_file: dict[tuple[str, str], str], target: tuple[str, str]
) -> str | None:
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


# ---------------------------------------------------------------------------
# Python module-path resolution.
# ---------------------------------------------------------------------------


def _py_path_to_module(path: Path, root: Path) -> str | None:
    """Convert ``pkg/sub/foo.py`` → ``pkg.sub.foo``. ``__init__.py`` collapses."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    parts = list(rel.parts)
    if not parts:
        return None
    last = parts[-1]
    if last == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = last.removesuffix(".py")
    return ".".join(parts) if parts else None


def _resolve_py(
    mod_to_file: dict[str, str], module: str
) -> str | None:
    """Try the exact module then walk up the dotted path."""
    while module:
        if module in mod_to_file:
            return mod_to_file[module]
        if "." not in module:
            return None
        module = module.rsplit(".", 1)[0]
    return None


# ---------------------------------------------------------------------------
# TS/JS resolution.
# ---------------------------------------------------------------------------

_TS_RESOLVE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _resolve_ts(spec: str, source_file: Path) -> str | None:
    """Resolve a relative TS/JS import ``spec`` against ``source_file``.

    Absolute imports (no leading ``./`` or ``../``) currently map to nothing —
    they typically refer to npm packages or path-aliased entries which we
    don't try to follow.
    """
    if not spec.startswith("."):
        return None
    base = (source_file.parent / spec).resolve()
    if base.is_file():
        return str(base)
    for ext in _TS_RESOLVE_EXTS:
        candidate = base.with_suffix(ext)
        if candidate.is_file():
            return str(candidate)
    # bare path: try base + ext
    for ext in _TS_RESOLVE_EXTS:
        candidate = Path(str(base) + ext)
        if candidate.is_file():
            return str(candidate)
    # directory index
    if base.is_dir():
        for ext in _TS_RESOLVE_EXTS:
            candidate = base / f"index{ext}"
            if candidate.is_file():
                return str(candidate)
    return None


# ---------------------------------------------------------------------------
# Edge extraction.
# ---------------------------------------------------------------------------


def _rust_edges(
    files: list[Path], root: Path
) -> list[tuple[str, str]]:
    mod_to_file: dict[tuple[str, str], str] = {}
    for f in files:
        crate, mod = _rust_path_to_module(f, root)
        if crate is None:
            continue
        mod_to_file[(crate, mod)] = str(f)
    edges: list[tuple[str, str]] = []
    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        crate, mod_local = _rust_path_to_module(f, root)
        crate_local = crate or ""
        for m in _RUST_USE_CRATE_RE.finditer(text):
            target = (crate_local, _trim_last_segment(m.group(1)))
            tgt = _resolve_rust(mod_to_file, target)
            if tgt and tgt != str(f):
                edges.append((str(f), tgt))
        for m in _RUST_USE_SELF_RE.finditer(text):
            target = (crate_local, _trim_last_segment(m.group(1)))
            tgt = _resolve_rust(mod_to_file, target)
            if tgt and tgt != str(f):
                edges.append((str(f), tgt))
        for m in _RUST_USE_EXTERN_RE.finditer(text):
            ext_crate = m.group(1)
            if ext_crate in _RUST_EXTERN_SKIP:
                continue
            target = (ext_crate, _trim_last_segment(m.group(2)))
            tgt = _resolve_rust(mod_to_file, target)
            if tgt and tgt != str(f):
                edges.append((str(f), tgt))
        for m in _RUST_MOD_RE.finditer(text):
            sub = m.group(1)
            target_mod = (
                f"{mod_local}::{sub}".lstrip(":") if mod_local else sub
            )
            target = (crate_local, target_mod)
            tgt = _resolve_rust(mod_to_file, target)
            if tgt and tgt != str(f):
                edges.append((str(f), tgt))
    return edges


def _python_edges(
    files: list[Path], root: Path
) -> list[tuple[str, str]]:
    mod_to_file: dict[str, str] = {}
    for f in files:
        mod = _py_path_to_module(f, root)
        if mod is not None:
            mod_to_file[mod] = str(f)
    edges: list[tuple[str, str]] = []
    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        own_mod = _py_path_to_module(f, root) or ""
        own_pkg = own_mod.rsplit(".", 1)[0] if "." in own_mod else ""
        # `from .x import …` and `from ..x import …`
        for m in _PY_FROM_RE.finditer(text):
            dots = m.group(1) or ""
            mod_part = m.group(2)
            if dots:
                # relative — climb own_pkg by len(dots)-1
                up = len(dots) - 1
                base = own_pkg.split(".") if own_pkg else []
                if up > len(base):
                    continue
                base = base[: len(base) - up] if up else base
                full = ".".join([*base, mod_part]) if mod_part else ".".join(base)
            else:
                full = mod_part
            tgt = _resolve_py(mod_to_file, full)
            if tgt and tgt != str(f):
                edges.append((str(f), tgt))
        # Bare `from . import foo` / `from .. import foo`
        for m in _PY_FROM_REL_RE.finditer(text):
            dots = m.group(1)
            up = len(dots) - 1
            base = own_pkg.split(".") if own_pkg else []
            if up > len(base):
                continue
            full = ".".join(base[: len(base) - up] if up else base)
            tgt = _resolve_py(mod_to_file, full) if full else None
            if tgt and tgt != str(f):
                edges.append((str(f), tgt))
        for m in _PY_IMPORT_RE.finditer(text):
            full = m.group(1)
            tgt = _resolve_py(mod_to_file, full)
            if tgt and tgt != str(f):
                edges.append((str(f), tgt))
    return edges


def _ts_edges(files: list[Path]) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for pattern in (_TS_IMPORT_FROM_RE, _TS_IMPORT_DYN_RE, _TS_REQUIRE_RE):
            for m in pattern.finditer(text):
                spec = m.group(1)
                tgt = _resolve_ts(spec, f)
                if tgt and tgt != str(f):
                    edges.append((str(f), tgt))
    return edges


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
    """Walk all source files under ``root`` and return per-file graph stats.

    Returned shape: ``{file_path: {"in_degree": int, "out_degree": int,
    "pagerank": float}}``.
    """

    root = Path(root).resolve()
    files = _collect_files(root)
    rust_files = [f for f in files if f.suffix in _RUST_EXT]
    py_files = [f for f in files if f.suffix in _PY_EXT]
    ts_files = [f for f in files if f.suffix in _TS_JS_EXT]

    edges: list[tuple[str, str]] = []
    if rust_files:
        edges.extend(_rust_edges(rust_files, root))
    if py_files:
        edges.extend(_python_edges(py_files, root))
    if ts_files:
        edges.extend(_ts_edges(ts_files))

    nodes = sorted({str(f) for f in files})
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
    """Build the export graph for ``root`` and write to ``file_graph``.

    Returns the number of rows inserted. Idempotent — clears the table
    before re-inserting so re-running on a moved repo is safe.
    """

    root = Path(root)
    graph = build_export_graph(root)
    conn.execute("DELETE FROM file_graph")
    rows = []
    for file_path, stats in graph.items():
        try:
            mtime = Path(file_path).stat().st_mtime
        except OSError:
            mtime = 0.0
        rows.append(
            (
                file_path,
                stats["in_degree"],
                stats["out_degree"],
                stats["pagerank"],
                mtime,
            )
        )
    conn.executemany(
        "INSERT INTO file_graph (file, in_degree, out_degree, pagerank, file_mtime) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)
