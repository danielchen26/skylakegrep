# SPDX-License-Identifier: Apache-2.0
"""Code reference extractor: Rust + Python + TS/JS regex-driven edges.

This module hosts the original ``code_graph`` extractors (formerly
``_rust_edges`` / ``_python_edges`` / ``_ts_edges``) reorganised behind the
unified ``extract_edges(files, root)`` plugin contract used by
``reference_graph``.

The actual regexes / module-resolution logic are mirrored verbatim from
the legacy ``code_graph.py`` so behaviour is byte-identical.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Languages we parse. Anything else is silently skipped.
# ---------------------------------------------------------------------------

RUST_EXT = {".rs"}
PY_EXT = {".py"}
TS_JS_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}

ALL_EXT = RUST_EXT | PY_EXT | TS_JS_EXT

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
# Rust module path resolution.
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
    if not spec.startswith("."):
        return None
    base = (source_file.parent / spec).resolve()
    if base.is_file():
        return str(base)
    for ext in _TS_RESOLVE_EXTS:
        candidate = base.with_suffix(ext)
        if candidate.is_file():
            return str(candidate)
    for ext in _TS_RESOLVE_EXTS:
        candidate = Path(str(base) + ext)
        if candidate.is_file():
            return str(candidate)
    if base.is_dir():
        for ext in _TS_RESOLVE_EXTS:
            candidate = base / f"index{ext}"
            if candidate.is_file():
                return str(candidate)
    return None


# ---------------------------------------------------------------------------
# Per-language edge extractors (private — kept for parity with legacy module).
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
        for m in _PY_FROM_RE.finditer(text):
            dots = m.group(1) or ""
            mod_part = m.group(2)
            if dots:
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
# Public plugin entrypoint — the reference_graph registry calls this.
# ---------------------------------------------------------------------------


def extract_edges(files: list[Path], root: Path) -> list[tuple[str, str]]:
    """Return ``(src, dst)`` edges for every code file in ``files``.

    Files whose suffix is not in ``ALL_EXT`` are silently skipped — the
    dispatcher in ``reference_graph`` already partitions by content type, so
    we only re-filter as a safety net.
    """

    rust_files = [f for f in files if f.suffix in RUST_EXT]
    py_files = [f for f in files if f.suffix in PY_EXT]
    ts_files = [f for f in files if f.suffix in TS_JS_EXT]

    edges: list[tuple[str, str]] = []
    if rust_files:
        edges.extend(_rust_edges(rust_files, root))
    if py_files:
        edges.extend(_python_edges(py_files, root))
    if ts_files:
        edges.extend(_ts_edges(ts_files))
    return edges
