"""Backward-compatible facade over ``reference_graph``.

This module used to host the Rust / Python / TS-JS edge extractors directly.
The implementation has moved to ``skylakegrep.src.extractors.code`` (the
"code" plugin in the content-agnostic ``reference_graph`` registry) so future
content types — markdown, YAML configs, knowledge graphs — can plug in
without touching retrieval code.

The public entry points (``build_export_graph``, ``populate_graph_table``)
are re-exported here so existing callers (``cli.populate_graph_table``,
``tests.test_code_graph``) keep working unchanged.

The legacy private symbols (``_rust_edges`` / ``_python_edges`` /
``_ts_edges``, the language regexes and helpers) are also re-exported because
some scripts under ``benchmarks/`` historically reached into them. New code
should import from ``skylakegrep.src.reference_graph`` instead.
"""

from __future__ import annotations

# Re-export the public API for callers that still import from this module.
from .reference_graph import (
    build_export_graph,
    populate_graph_table,
    REFERENCE_EXTRACTORS,
    register_extractor,
    _pagerank,
    _is_ignored,
    _IGNORED_DIRS,
)

# Re-export the legacy private code-extractor internals so existing benchmark
# scripts that import them keep functioning. New callers should reach into
# ``skylakegrep.src.extractors.code`` directly.
from .extractors.code import (
    RUST_EXT as _RUST_EXT,
    PY_EXT as _PY_EXT,
    TS_JS_EXT as _TS_JS_EXT,
    _rust_path_to_module,
    _trim_last_segment,
    _resolve_rust,
    _py_path_to_module,
    _resolve_py,
    _resolve_ts,
    _rust_edges,
    _python_edges,
    _ts_edges,
)

__all__ = [
    "build_export_graph",
    "populate_graph_table",
    "REFERENCE_EXTRACTORS",
    "register_extractor",
]


def _collect_files(root):
    """Legacy helper: code-only file collection.

    Preserved for the small handful of benchmark scripts that import this
    name directly; the real walk now lives in ``reference_graph``.
    """
    from pathlib import Path
    exts = _RUST_EXT | _PY_EXT | _TS_JS_EXT
    files: list[Path] = []
    for p in Path(root).rglob("*"):
        if not p.is_file():
            continue
        if p.suffix not in exts:
            continue
        if _is_ignored(p.relative_to(root)):
            continue
        files.append(p)
    return files
