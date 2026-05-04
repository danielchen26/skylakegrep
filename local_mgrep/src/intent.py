"""Intent classification + multi-tier result merging.

v0.14.0 changes the routing model from *mutually-exclusive tiers*
(only one of filename / lexical / cascade runs) to *hierarchical
merge* (all enabled tiers contribute, ranked by detected intent).

The CLI runs every enabled tier — filename, lexical, semantic
cascade — collects their results, deduplicates by path, and ranks
the merged list using ``classify_intent(query)`` to decide which
tier wins ties.

Why it matters: a query like ``where is config file?`` may have
both an obvious filename match (`config.py`) and useful semantic
context (the chunk where config is loaded from environment vars).
v0.13.0 returned only the filename match. v0.14.0 surfaces both,
with the filename match pinned to the top because the query
phrasing makes filename intent dominant.
"""

from __future__ import annotations

import re
from typing import Iterable

# Phrases that signal a clear filename-lookup intent
_FILENAME_INTENT_PHRASES = (
    "find ", "where is", "where's", "locate ", "show me",
    "look for", "search for", "open ",
)

# Words/tokens that suggest descriptive semantic question
_SEMANTIC_INTENT_WORDS = frozenset({
    "how", "why", "what", "explain", "describe",
    "implement", "implementation", "logic", "behaviour",
    "behavior", "flow", "pipeline", "purpose", "reason",
    "decide", "decides", "handle", "handles", "process",
    "calls", "called", "invoked", "trigger", "triggers",
})

# Standalone "file/files" or extension hint pushes toward filename
_EXT_RE = re.compile(r"\b\w+\.(?:py|js|ts|tsx|rs|go|md|json|yaml|yml|toml|html|svg|pdf|docx|txt|sh|c|cpp|h|hpp|java|kt|swift|sql)\b")


def classify_intent(query: str) -> str:
    """Classify a query into one of four intent buckets.

    Returns one of:
      - ``"filename"`` — query clearly asks for a file by name
        (``where is X file``, ``find foo.py``).
      - ``"semantic"`` — descriptive natural-language question about
        code behaviour (``how does the cascade decide`` etc.).
      - ``"lexical"`` — short query of code-token-like words that
        reads more like a literal grep (``auth login token``).
      - ``"mixed"`` — fallthrough; no strong signal in either direction.
    """
    if not query or not query.strip():
        return "mixed"
    q_lower = query.lower()

    has_filename_phrase = any(p in q_lower for p in _FILENAME_INTENT_PHRASES)
    has_explicit_ext = bool(_EXT_RE.search(query))
    if has_filename_phrase or has_explicit_ext:
        return "filename"

    # Strong descriptive signals → semantic
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_]*", q_lower)
    if any(t in _SEMANTIC_INTENT_WORDS for t in tokens):
        return "semantic"

    # Long query (> 6 non-trivial words) is likely descriptive
    meaningful = [t for t in tokens if len(t) > 2]
    if len(meaningful) >= 6:
        return "semantic"

    # Short query of code-tokens → lexical
    if 1 <= len(meaningful) <= 4:
        return "lexical"

    return "mixed"


# Tier priority by intent: lower number = higher rank
_TIER_PRIORITY: dict[str, dict[str, int]] = {
    # Filename intent: filename matches dominate, then content
    "filename":  {"filename-lookup": 0, "rg-shortcut": 1, "cascade": 2},
    # Semantic intent: cascade dominates, content lexical second,
    # filename last (still surfaced for context)
    "semantic":  {"cascade": 0, "rg-shortcut": 1, "filename-lookup": 2},
    # Lexical intent: rg-shortcut content match dominates
    "lexical":   {"rg-shortcut": 0, "cascade": 1, "filename-lookup": 2},
    # Mixed: all tiers equal — tie broken by raw score
    "mixed":     {"filename-lookup": 0, "rg-shortcut": 0, "cascade": 0},
}


def _tier_of(r: dict) -> str:
    return r.get("fallback") or "cascade"


def merge_results(
    *,
    filename: Iterable[dict] | None,
    lexical: Iterable[dict] | None,
    semantic: Iterable[dict] | None,
    intent: str,
    top_k: int,
) -> list[dict]:
    """Dedupe by path, rank by (intent-priority, -score), return top-k.

    Each input may be ``None`` or empty; missing tiers are simply
    skipped. The first occurrence of each path wins (after sorting
    so the best-tier representation lands first).
    """
    pri = _TIER_PRIORITY.get(intent, _TIER_PRIORITY["mixed"])

    pool: list[dict] = []
    for batch in (filename or [], lexical or [], semantic or []):
        pool.extend(batch)

    # Dedupe by path: among multiple appearances of the same path,
    # keep the one with the best (lowest) tier priority for this
    # intent; tie-break by higher score.
    by_path: dict[str, dict] = {}
    for r in pool:
        path = r.get("path", "")
        if not path:
            continue
        if path not in by_path:
            by_path[path] = r
            continue
        existing = by_path[path]
        new_pri = pri.get(_tier_of(r), 99)
        old_pri = pri.get(_tier_of(existing), 99)
        if new_pri < old_pri:
            by_path[path] = r
        elif new_pri == old_pri:
            if (r.get("score") or 0.0) > (existing.get("score") or 0.0):
                by_path[path] = r

    # Final rank: tier priority first, then score descending
    def _rank_key(r: dict) -> tuple:
        return (
            pri.get(_tier_of(r), 99),
            -float(r.get("score") or 0.0),
        )

    ranked = sorted(by_path.values(), key=_rank_key)
    return ranked[:top_k]
