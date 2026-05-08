"""Fast filesystem-metadata answers for queries that are not content search.

Semantic retrieval is the wrong tool for questions such as "latest files I
opened": the answer lives in filesystem timestamps, not file contents. This
module keeps that path local, bounded, and cheap so those queries do not fall
into cold-start lazy embedding.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import time

from .fast_intent import classify_fast_intent


@dataclass(frozen=True)
class MetadataQuery:
    kind: str
    limit: int
    reason: str


_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cursor", ".cache",
    "Library", "Applications",
}


def classify_metadata_query(query: str, *, default_limit: int = 5) -> MetadataQuery | None:
    q = (query or "").strip()
    if not q:
        return None

    fast = classify_fast_intent(q)
    if fast is None or fast.intent != "metadata":
        return None
    kind = (
        fast.primary_token
        if fast.primary_token in {"opened", "modified", "size"}
        else ""
    )
    if not kind:
        return None

    limit = _limit_from_query(q, default_limit=default_limit)
    return MetadataQuery(
        kind=kind,
        limit=limit,
        reason=fast.reason,
    )


def _limit_from_query(query: str, *, default_limit: int) -> int:
    m = re.search(r"\b(\d{1,3})\b", query)
    if not m:
        return max(1, min(default_limit, 50))
    return max(1, min(int(m.group(1)), 50))


def metadata_results(
    query: str,
    root: Path,
    *,
    top_k: int = 5,
    max_files: int = 25_000,
) -> tuple[list[dict], MetadataQuery | None]:
    meta = classify_metadata_query(query, default_limit=top_k)
    if meta is None:
        return [], None

    limit = min(meta.limit, top_k if top_k > 0 else meta.limit)
    key = "atime" if meta.kind == "opened" else (
        "size" if meta.kind == "size" else "mtime"
    )
    candidates: list[tuple[float, Path, os.stat_result]] = []
    scanned = 0
    for base in _search_roots(root):
        for path in _iter_files(base):
            try:
                st = path.stat()
            except OSError:
                continue
            scanned += 1
            ts = st.st_atime if key == "atime" else (
                float(st.st_size) if key == "size" else st.st_mtime
            )
            candidates.append((ts, path, st))
            if scanned >= max_files:
                break
        if scanned >= max_files:
            break

    candidates.sort(key=lambda item: item[0], reverse=True)
    results: list[dict] = []
    for ts, path, st in candidates[:limit]:
        label = "size" if meta.kind == "size" else (
            "last opened" if meta.kind == "opened" else "modified"
        )
        mtime = _fmt_time(st.st_mtime)
        atime = _fmt_time(st.st_atime)
        value = _fmt_size(st.st_size) if meta.kind == "size" else _fmt_time(ts)
        snippet = (
            f"{label}: {value}    modified: {mtime}    "
            f"opened: {atime}    size: {_fmt_size(st.st_size)}"
        )
        results.append(
            {
                "path": str(path),
                "file": str(path),
                "chunk": snippet,
                "snippet": snippet,
                "language": path.suffix.lstrip(".") or "file",
                "start_line": None,
                "end_line": None,
                "score": 1.0,
                "fallback": f"metadata-{meta.kind}",
                "metadata_kind": meta.kind,
                "metadata_reason": meta.reason,
            }
        )
    return results, meta


def _search_roots(root: Path) -> list[Path]:
    root = root.expanduser().resolve()
    home = Path.home().resolve()
    if root == home:
        preferred = [
            home / "Downloads",
            home / "Desktop",
            home / "Documents",
        ]
        return [p for p in preferred if p.exists() and p.is_dir()] or [root]
    return [root]


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".")
        ]
        for name in filenames:
            if name.startswith("."):
                continue
            yield Path(dirpath) / name


def _fmt_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"
