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
from .query_scope import strip_scope_clauses


@dataclass(frozen=True)
class MetadataQuery:
    kind: str
    limit: int
    reason: str


@dataclass(frozen=True)
class MetadataFacet:
    kind: str
    limit: int
    terminal: bool
    target_descriptors: tuple[str, ...]
    reason: str


_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cursor", ".cache",
    "Library", "Applications",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,80}")
_CJK_RUN_RE = re.compile(r"[\u3400-\u9fff]{2,80}")
_TEMPORAL_CONTEXT_RE = re.compile(
    r"\b(recent|recently|latest|newest|oldest|last|today|yesterday)\b",
    re.IGNORECASE,
)
_METADATA_KINDS = {"opened", "modified", "created", "size"}

# Generic grammar / metadata words that do not identify a target artifact.
# This is intentionally domain-free: it strips the query scaffolding and the
# timestamp/size ask, then checks whether meaningful descriptors remain. If
# descriptors remain, metadata can be a ranking/filter signal, but not the
# whole answer.
_NON_DESCRIPTOR_TOKENS = frozenset({
    "a", "an", "all", "and", "any", "are", "as", "at", "by", "did", "do",
    "does", "for", "from", "give", "i", "in", "into", "is", "it", "me",
    "my", "of", "on", "or", "please", "show", "some", "tell", "that",
    "the", "these", "this", "those", "to", "was", "were", "what", "when",
    "where", "which", "with", "you",
    "file", "files", "folder", "folders", "document", "documents",
    "repo", "repository", "project", "workspace", "directory",
    "latest", "recent", "recently", "newest", "oldest", "last", "first",
    "most", "least", "top", "bottom",
    "opened", "accessed", "used", "created", "made", "wrote", "written",
    "modified", "changed", "edited", "updated", "largest", "smallest",
    "biggest", "size", "sizes", "today", "yesterday", "week", "month",
    "year",
})

_CJK_NON_DESCRIPTOR_FRAGMENTS = (
    "我", "我的", "我们", "請", "请", "帮我", "给我", "显示", "列出",
    "哪里", "在哪", "在哪儿", "是什么", "什么", "哪个", "那些", "这个",
    "那个", "文件", "文档", "资料", "目录", "文件夹", "项目",
    "最近", "刚刚", "刚", "今天", "昨天", "上周", "这周", "这个月",
    "打开", "访问", "用过", "使用", "修改", "编辑", "更新", "创建",
    "新建", "写", "写的", "过的", "过", "的", "最大", "最小", "大小",
)


def _descriptor_tokens(query: str) -> list[str]:
    """Return tokens that look like target constraints, not metadata grammar.

    Metadata-only answers are correct for "latest files I opened". They are
    wrong for "where is the report I recently created in project X": the
    timestamp word is only a modifier, and the report/project terms must
    constrain retrieval. This generic residual check prevents the metadata
    lane from hijacking such composite searches.
    """

    scoped_query = strip_scope_clauses(query or "")
    out: list[str] = []
    for raw in _TOKEN_RE.findall(scoped_query):
        token = raw.strip("._-").lower()
        if len(token) < 2:
            continue
        if token.isdigit():
            continue
        if token in _NON_DESCRIPTOR_TOKENS:
            continue
        out.append(token)
    cjk_query = scoped_query
    for fragment in _CJK_NON_DESCRIPTOR_FRAGMENTS:
        cjk_query = cjk_query.replace(fragment, " ")
    for raw in _CJK_RUN_RE.findall(cjk_query):
        token = raw.strip()
        if len(token) >= 2:
            out.append(token)
    return out


def analyze_metadata_query(query: str, *, default_limit: int = 5) -> MetadataFacet | None:
    """Return the filesystem-metadata facet expressed by ``query``.

    The key distinction is terminal vs modifier:

    - terminal: metadata alone can answer the query ("latest opened files")
    - modifier: metadata constrains another target/content search
      ("the report I recently created")

    This keeps metadata as a plan facet instead of a mutually exclusive
    routing intent, so a timestamp phrase cannot suppress semantic depth.
    """

    q = (query or "").strip()
    if not q:
        return None

    fast = classify_fast_intent(q)
    if fast is None or fast.intent != "metadata":
        return None
    kind = (
        fast.primary_token
        if fast.primary_token in _METADATA_KINDS
        else ""
    )
    if not kind:
        return None

    descriptors = _descriptor_tokens(q)
    if _looks_like_code_identifier_collision(q, descriptors):
        return None
    limit = _limit_from_query(q, default_limit=default_limit)
    return MetadataFacet(
        kind=kind,
        limit=limit,
        terminal=not descriptors,
        target_descriptors=tuple(descriptors[:12]),
        reason=fast.reason,
    )


def _looks_like_code_identifier_collision(query: str, descriptors: list[str]) -> bool:
    """Avoid treating identifier names like ``created_at`` as metadata asks."""

    if not descriptors:
        return False
    has_structural_identifier = any(
        any(ch in token for ch in "._-/") for token in descriptors
    )
    if not has_structural_identifier:
        return False
    return _TEMPORAL_CONTEXT_RE.search(query or "") is None


def classify_metadata_query(query: str, *, default_limit: int = 5) -> MetadataQuery | None:
    facet = analyze_metadata_query(query, default_limit=default_limit)
    if facet is None or not facet.terminal:
        return None
    return MetadataQuery(
        kind=facet.kind,
        limit=facet.limit,
        reason=facet.reason,
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
    candidates: list[tuple[float, Path, os.stat_result]] = []
    scanned = 0
    for base in _search_roots(root):
        for path in _iter_files(base):
            try:
                st = path.stat()
            except OSError:
                continue
            scanned += 1
            ts = _stat_value(st, meta.kind)
            candidates.append((ts, path, st))
            if scanned >= max_files:
                break
        if scanned >= max_files:
            break

    candidates.sort(key=lambda item: item[0], reverse=True)
    results: list[dict] = []
    for ts, path, st in candidates[:limit]:
        label = _kind_label(meta.kind)
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


def rank_results_by_metadata(results: list[dict], kind: str) -> list[dict]:
    """Apply a cheap metadata facet to an already-relevant result set.

    This never scans the filesystem. It stats only the paths that retrieval
    already found, then adds a small normalized boost so recency/size can
    break ties without letting unrelated files bypass semantic relevance.
    """

    if kind not in _METADATA_KINDS or not results:
        return results
    values: list[tuple[int, float]] = []
    for idx, result in enumerate(results):
        raw_path = result.get("path") or result.get("file")
        if not raw_path:
            continue
        try:
            st = Path(raw_path).stat()
        except OSError:
            continue
        values.append((idx, _stat_value(st, kind)))
    if not values:
        return results

    raw_vals = [v for _, v in values]
    lo = min(raw_vals)
    hi = max(raw_vals)
    spread = hi - lo
    normalized = {
        idx: (0.5 if spread <= 0 else (value - lo) / spread)
        for idx, value in values
    }

    ranked: list[tuple[float, int, dict]] = []
    for idx, result in enumerate(results):
        try:
            base = float(result.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            base = 0.0
        # Small enough not to swamp real retrieval scores, large enough to
        # order equal-score anchors and lexical hits by the user's facet.
        adjusted = base + 0.04 * normalized.get(idx, 0.0)
        ranked.append((adjusted, -idx, result))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in ranked]


def _stat_value(st: os.stat_result, kind: str) -> float:
    if kind == "opened":
        return float(st.st_atime)
    if kind == "size":
        return float(st.st_size)
    if kind == "created":
        return float(getattr(st, "st_birthtime", st.st_mtime))
    return float(st.st_mtime)


def _kind_label(kind: str) -> str:
    if kind == "size":
        return "size"
    if kind == "opened":
        return "last opened"
    if kind == "created":
        return "created"
    return "modified"


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
