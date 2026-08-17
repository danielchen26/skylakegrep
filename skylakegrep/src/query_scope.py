"""Query-derived filesystem scope constraints.

This module handles query-plan facets such as "in PROJECT folder" or
"under reports directory". The key point is that scope is not an intent:
it constrains every retrieval lane. If the user names a folder, searching
the whole home tree first is both slower and less accurate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import re
import subprocess


@dataclass(frozen=True)
class ScopeFacet:
    root: Path
    label: str
    confidence: float
    reason: str


_SCOPE_RE = re.compile(
    r"(?:\b(?:in|inside|under|within|from|of)\s+|(?:在|于|於|从|從)\s*)"
    r"(?P<label>[\w\u3400-\u9fff][\w\u3400-\u9fff._ -]{0,80}?)\s*"
    r"(?:folder|directory|dir|repo|repository|project|workspace|"
    r"文件夹|資料夾|资料夹|目录|目錄|项目|專案|工程)"
    r"(?=$|[\s,.;:!?，。；：！？]|[\u3400-\u9fff])",
    re.IGNORECASE | re.UNICODE,
)
_TRAILING_FILLER_RE = re.compile(
    r"\b(?:that|which|where|i|me|my|recently|created|modified|opened|file|files|show|find)\b.*$",
    re.IGNORECASE,
)
_BAD_DIR_NAMES = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".cache", ".cursor", ".claude", ".gemini", ".antigravity",
    "Library", "Caches", "Applications", ".Trash",
}


def resolve_scope_facet(
    query: str,
    current_root: Path,
    *,
    max_candidates: int = 24,
) -> ScopeFacet | None:
    """Resolve a query's folder constraint to a concrete local root.

    The extraction is grammar-level and domain-free. The validation is
    filesystem-grounded: a scope exists only if we can find a matching
    directory. Hidden/tool/cache paths are demoted or filtered so they do
    not beat normal user/project folders.
    """

    label = _scope_label(query)
    if not label:
        return None
    candidates = _candidate_dirs(label, current_root, max_candidates=max_candidates)
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda p: _scope_rank(p, label, current_root),
    )
    root = ranked[0].resolve()
    confidence = _scope_confidence(root, label)
    if confidence < 0.55:
        return None
    return ScopeFacet(
        root=root,
        label=label,
        confidence=confidence,
        reason="query names a concrete folder scope",
    )


def _scope_label(query: str) -> str:
    q = (query or "").strip()
    if not q:
        return ""
    matches = list(_SCOPE_RE.finditer(q))
    if not matches:
        return ""
    raw = matches[-1].group("label").strip(" ._-")
    raw = _TRAILING_FILLER_RE.sub("", raw).strip(" ._-")
    parts = raw.split()
    # Keep the rightmost compact descriptor. In "my PROJECT folder", "my"
    # is ownership grammar and PROJECT is the actual filesystem clue.
    while len(parts) > 1 and parts[0].casefold() in {"my", "the", "a", "an"}:
        parts.pop(0)
    return " ".join(parts).strip(" ._-")


def strip_scope_clauses(query: str) -> str:
    """Remove explicit folder-scope clauses from a query string.

    Scope is its own query-plan facet. Other facet detectors, especially
    metadata, should not mistake the scope label for the target artifact.
    """

    return " ".join(_SCOPE_RE.sub(" ", query or "").split())


def _candidate_dirs(
    label: str,
    current_root: Path,
    *,
    max_candidates: int,
) -> list[Path]:
    roots = _search_roots(current_root)
    out: list[Path] = []
    seen: set[str] = set()
    for path in _direct_dirs(label, current_root):
        try:
            resolved = path.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        out.append(resolved)
        if len(out) >= max_candidates:
            return out
    if out:
        return out
    for root in roots:
        for path in _find_dirs(root, label, max_candidates=max_candidates):
            try:
                resolved = path.resolve()
            except OSError:
                continue
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            out.append(resolved)
            if len(out) >= max_candidates:
                return out
    return out


def _search_roots(current_root: Path) -> list[Path]:
    current_root = current_root.expanduser().resolve()
    home = Path.home().resolve()
    roots: list[Path] = []
    if current_root != home and current_root.exists() and current_root.is_dir():
        roots.append(current_root)
    if current_root == home:
        for child in (
            home / "Documents" / "GitHub",
            home / "Documents" / "GitHub 2",
            home / "Documents",
            home / "Desktop",
            home / "Downloads",
            home / "Code",
            home / "Projects",
        ):
            if child.is_dir():
                roots.append(child)
    else:
        for child in (
            current_root.parent,
            home / "Documents",
            home / "Desktop",
            home / "Downloads",
        ):
            if child.is_dir():
                roots.append(child)
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def _direct_dirs(label: str, current_root: Path) -> list[Path]:
    """Cheap exact/near-exact checks before any recursive walk."""

    home = Path.home().resolve()
    current_root = current_root.expanduser().resolve()
    bases = [
        current_root,
        current_root / "GitHub",
        home / "Documents" / "GitHub",
        home / "Documents" / "GitHub 2",
        home / "Documents",
        home / "Desktop",
        home / "Downloads",
        home / "Code",
        home / "Projects",
    ]
    label_cf = label.casefold()
    out: list[Path] = []
    seen: set[str] = set()
    for base in bases:
        if not base.is_dir():
            continue
        for child in [base / label]:
            if child.is_dir():
                key = str(child)
                if key not in seen:
                    seen.add(key)
                    out.append(child)
        try:
            children = list(base.iterdir())
        except OSError:
            continue
        for child in children:
            if not child.is_dir() or _is_ignored_name(child.name):
                continue
            name_cf = child.name.casefold()
            if name_cf == label_cf or label_cf in name_cf:
                key = str(child)
                if key not in seen:
                    seen.add(key)
                    out.append(child)
    return out


def _find_dirs(root: Path, label: str, *, max_candidates: int) -> list[Path]:
    find = shutil_which_find()
    pattern = f"*{label}*"
    if find:
        # Prune relative to ``root`` (``find .`` with ``cwd=root``) so the
        # ``*/.*`` hidden-path prune only applies to directories *inside*
        # ``root``. With an absolute root, the prune matched ``root``'s own
        # hidden ancestors and pruned the entire search.
        cmd = [
            find, ".",
            "-maxdepth", "5",
            "(",
            "-path", "*/.*",
            "-o", "-path", "*/Library/*",
            "-o", "-path", "*/Caches/*",
            "-o", "-path", "*/node_modules/*",
            "-o", "-path", "*/.venv/*",
            ")",
            "-prune",
            "-o",
            "-type", "d",
            "-iname", pattern,
            "-print",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=0.5,
                cwd=str(root),
            )
        except (OSError, subprocess.TimeoutExpired):
            proc = None
        if proc is not None and proc.returncode in {0, 1}:
            found: list[Path] = []
            for line in proc.stdout.splitlines():
                s = line.strip()
                if not s or s == ".":
                    continue
                # Re-absolutise; callers compare against real paths.
                found.append(root / s[2:] if s.startswith("./") else root / s)
            return found[:max_candidates]

    out: list[Path] = []
    label_cf = label.casefold()
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_ignored_name(d)]
        rel_depth = len(Path(dirpath).relative_to(root).parts)
        if rel_depth >= 5:
            dirnames[:] = []
        for d in dirnames:
            if label_cf in d.casefold():
                out.append(Path(dirpath) / d)
                if len(out) >= max_candidates:
                    return out
    return out


def shutil_which_find() -> str | None:
    try:
        import shutil
        return shutil.which("find")
    except Exception:
        return None


def _scope_rank(path: Path, label: str, current_root: Path) -> tuple:
    parts = path.parts
    name = path.name.casefold()
    label_cf = label.casefold()
    hidden_penalty = 1 if any(_is_ignored_name(part) for part in parts) else 0
    exact_penalty = 0 if name == label_cf else 1
    git_penalty = 0 if (path / ".git").exists() else 1
    copy_penalty = sum(
        1
        for part in parts
        if re.search(r"(?: copy| copy \d+| \d+)$", part, re.IGNORECASE)
    )
    depth = len(path.parts)
    try:
        current_root = current_root.resolve()
        under_current = 0 if path.resolve().is_relative_to(current_root) else 1
    except Exception:
        under_current = 1
    return (
        hidden_penalty,
        exact_penalty,
        git_penalty,
        copy_penalty,
        under_current,
        depth,
        str(path),
    )


def _scope_confidence(path: Path, label: str) -> float:
    name = path.name.casefold()
    label_cf = label.casefold()
    if name == label_cf:
        return 0.95
    if label_cf in name:
        return 0.8
    return 0.5


def _is_ignored_name(name: str) -> bool:
    return name.startswith(".") or name in _BAD_DIR_NAMES
