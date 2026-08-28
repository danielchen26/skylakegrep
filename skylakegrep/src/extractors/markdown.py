# SPDX-License-Identifier: Apache-2.0
"""Markdown reference extractor: parse ``[text](target)`` and wikilinks.

This is the proof-of-concept second plugin for the content-agnostic
reference-extractor registry. It parses two link forms:

  * Standard markdown ``[text](target)`` — ``target`` may be relative path,
    URL, or in-document anchor (``#anchor``).
  * Wiki-style ``[[target]]`` (Obsidian / many static-site generators).

Resolution rules:
  * Pure anchors (``#section``) and absolute URLs (``http://``, ``https://``,
    ``mailto:``, etc.) are dropped — they don't form file-to-file edges.
  * Relative paths (``./other.md``, ``../docs/x.md``) resolve against the
    source file's directory.
  * Targets without an extension that resolve to a file with ``.md`` or
    ``.mdx`` appended are accepted (Obsidian convention).
  * The trailing ``#anchor`` on otherwise-relative targets is stripped before
    resolution.
"""

from __future__ import annotations

import re
from pathlib import Path

MARKDOWN_EXT = {".md", ".mdx", ".markdown"}

# [text](target) — non-greedy text, target stops at first whitespace, quote, or
# closing paren. Allow images ``![alt](src)`` too (the leading ``!`` is fine).
_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(\s*([^)\s'\"<>]+)")
# Wiki/Obsidian style.
_MD_WIKI_RE = re.compile(r"\[\[([^\]\n|#]+)(?:#[^\]\n|]*)?(?:\|[^\]\n]*)?\]\]")

_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")
_RESOLVE_EXTS = (".md", ".mdx", ".markdown")


def _is_external(target: str) -> bool:
    """External URL or pure anchor — never a file-to-file edge."""
    if not target:
        return True
    if target.startswith("#"):
        return True
    # protocol://... or mailto:..., but not Windows drive letters that we'd
    # ever see in markdown link targets.
    return bool(_URL_SCHEME_RE.match(target))


def _resolve_target(target: str, source_file: Path) -> str | None:
    """Resolve ``target`` (a markdown link href) to an absolute file path.

    Returns ``None`` if the target points at a non-existent file or is
    external (URL / pure anchor). The trailing ``#anchor`` is stripped.
    """

    if _is_external(target):
        return None
    # Strip ``?query`` and ``#anchor``.
    href = target.split("#", 1)[0].split("?", 1)[0]
    if not href:
        return None

    base = (source_file.parent / href).resolve()
    if base.is_file():
        return str(base)
    # No-extension wiki style: try .md / .mdx / .markdown.
    if not base.suffix:
        for ext in _RESOLVE_EXTS:
            cand = Path(str(base) + ext)
            if cand.is_file():
                return str(cand)
        # Directory index convention: ``other_doc/`` → ``other_doc/index.md``.
        if base.is_dir():
            for ext in _RESOLVE_EXTS:
                cand = base / f"index{ext}"
                if cand.is_file():
                    return str(cand)
    return None


def extract_edges(files: list[Path], root: Path) -> list[tuple[str, str]]:
    """Return ``(src, dst)`` edges for every markdown link found.

    Both ``src`` and ``dst`` are absolute paths (``str``) resolved on disk;
    targets that don't resolve to an existing file are dropped, mirroring
    the code extractor's behaviour for unresolved imports. ``root`` is
    accepted for API symmetry with the code extractor — markdown resolution
    is purely relative to each source file's directory and ignores ``root``.
    """

    del root  # unused — markdown links resolve relative to source file
    edges: list[tuple[str, str]] = []
    for f in files:
        if f.suffix not in MARKDOWN_EXT:
            continue
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        src = str(f)
        for m in _MD_LINK_RE.finditer(text):
            target = m.group(2)
            dst = _resolve_target(target, f)
            if dst and dst != src:
                edges.append((src, dst))
        for m in _MD_WIKI_RE.finditer(text):
            target = m.group(1).strip()
            dst = _resolve_target(target, f)
            if dst and dst != src:
                edges.append((src, dst))
    return edges
