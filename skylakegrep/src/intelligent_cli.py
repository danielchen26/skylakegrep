"""Intelligent CLI assistance — proactive guidance for the user.

Four kinds of help, all non-blocking and disable-able via
``SKYGREP_NO_HINTS=1`` for users / CI that want quiet output:

1. **Out-of-scope query detection.** A query like
   "我最近工作上的十个文件" / "list the largest 5 files" is a
   *metadata* query — it wants filesystem mtime / size sort, not
   content search. skygrep can't answer it well; the user wants
   ``git log --name-only`` or ``find -mtime``. We detect the
   pattern up front and print a hint with the right command,
   then still run the search so we don't block the user.

2. **First-run nudge.** When a user runs ``skygrep`` against a
   project with no existing index, the first query falls back to
   rg in < 1 s while a background worker indexes — but the user
   doesn't know that, and may think the tool is broken or slow.
   We surface a one-time three-line greeting that explains what's
   happening and points at ``skygrep doctor`` / ``skygrep setup``
   for the next steps.

3. **Low-confidence result hints.** When the cascade returns a
   top-1 score below floor and a σ-gap below floor, we know the
   answer is shaky. Suggest a recovery path — ``--agentic``,
   ``--top 30``, more specific tokens — so the user doesn't quit
   thinking the tool failed.

4. **Typo correction for unknown commands and flags.** When the
   user types ``skygrep serach`` or ``skygrep search --tup 10``,
   click's default error is "Usage: …" with the full help —
   nothing telling them they probably meant ``search`` or
   ``--top``. We catch the click exception, run ``difflib.get_
   close_matches`` against the known set, and suggest the closest
   match.

The module deliberately avoids any heavy imports (no requests,
no sqlite3 at import time) so it stays cheap to load on every
``skygrep …`` invocation. The single ``sqlite3`` use lives inside
``should_show_first_run_nudge`` and only runs when the caller
actually calls it.
"""

from __future__ import annotations

import difflib
import os
import re
import sqlite3
from typing import Any, Iterable, Optional


# ---------------------------------------------------------------------------
# Out-of-scope query detection
# ---------------------------------------------------------------------------

# Tokens that strongly imply the user wants filesystem metadata
# (mtime sort, size sort, listing) rather than semantic content search.
# A query that contains one of these AND no semantic-intent token
# (below) is flagged.
_METADATA_TOKENS = {
    # English — mtime / recency (absolute and relative)
    "recent", "latest", "newest", "oldest", "lately",
    "yesterday", "today", "this morning", "last week", "this week",
    # English — size
    "largest", "biggest", "smallest", "tiniest",
    # English — listing / counting
    "all", "list", "count", "every", "all the",
    # English — quantifiers that pair with file metadata
    "how many",
    # Chinese — mtime / recency (absolute and day-relative — added 0.2.5
    # after the 昨天 miss reported on the 我昨天打开过的十个文件 query)
    "最近", "最新", "最旧", "新近", "近期",
    "昨天", "今天", "前天", "上周", "本周",
    "刚刚",
    # Chinese — size
    "最大", "最小", "最长", "最短",
    # Chinese — listing / counting
    "列出", "列举", "所有", "全部", "几个", "多少",
    # Chinese — sorting
    "排序",
    # Chinese — verbs that imply mtime ("opened", "edited"; pure
    # filesystem-event vocab the ``find -mtime`` family answers)
    "打开过", "改过", "编辑过", "修改过",
}

# If a query contains one of these, it is asking about *content* / *intent*
# even if it also mentions a metadata token, so we DO NOT flag it as
# out-of-scope. Example: "where is the recent change to auth flow" → has
# "recent" but is a semantic query about a specific behavior.
_SEMANTIC_INTENT_TOKENS = {
    # English — interrogatives
    "how", "where", "why", "what", "which", "explain", "show me",
    # English — content nouns
    "function", "method", "class", "implementation", "logic", "flow",
    "called", "definition", "defined", "definitions", "logic",
    # English — verbs that imply implementation
    "implements", "handles", "computes", "calls", "invokes", "uses",
    # Chinese — interrogatives
    "怎么", "为什么", "如何", "哪里", "什么", "哪个",
    # Chinese — content nouns
    "函数", "类", "方法", "实现", "逻辑", "调用", "定义",
}


def detect_out_of_scope(query: str) -> Optional[dict]:
    """Decide whether ``query`` looks like a metadata query (mtime /
    size / listing / counting) that skygrep is the wrong tool for.

    Returns ``None`` for content-search queries (the common case).
    Returns a dict with ``reason`` and ``suggested_command`` when
    the query is flagged so the caller can render a hint.

    Conservative: requires a metadata token AND no semantic-intent
    token AND a short query (≤ 12 words). This avoids flagging
    legitimate semantic queries that happen to mention a metadata
    word in passing.
    """

    q = query.strip().lower()
    if not q:
        return None
    # Word count by whitespace works for English; CJK characters are
    # treated as one "word" each by counting code points where there's
    # no whitespace. The 12-word ceiling is generous for short queries
    # but rules out long sentences which are usually content queries.
    word_count = len(q.split()) if any(c == " " for c in q) else len(q)
    if word_count > 12:
        return None
    if any(tok in q for tok in _SEMANTIC_INTENT_TOKENS):
        return None
    matched = None
    for tok in _METADATA_TOKENS:
        if tok in q:
            matched = tok
            break
    if matched is None:
        return None

    # Pick a suggestion based on the kind of metadata token. Cover the
    # three common families: recency / size / listing-and-counting.
    if matched in {
        "recent", "latest", "newest", "oldest", "lately",
        "yesterday", "today", "this morning", "last week", "this week",
        "最近", "最新", "最旧", "新近", "近期",
        "昨天", "今天", "前天", "上周", "本周", "刚刚",
        "打开过", "改过", "编辑过", "修改过",
    }:
        return {
            "reason": f"contains '{matched}' (recency-by-mtime)",
            "suggested_command": (
                "git log --name-only --pretty=format: HEAD~30..HEAD | sort -u | head -10"
            ),
            "alt_commands": [
                "find . -type f -mtime -7 -not -path '*/.*'",
                "git diff --name-only HEAD~10..HEAD",
            ],
        }
    if matched in {"oldest", "最旧"}:
        return {
            "reason": f"contains '{matched}' (oldest-by-mtime)",
            "suggested_command": "find . -type f -printf '%T+ %p\\n' | sort | head",
            "alt_commands": [],
        }
    if matched in {
        "largest", "biggest", "smallest", "tiniest",
        "最大", "最小", "最长", "最短",
    }:
        return {
            "reason": f"contains '{matched}' (size sort)",
            "suggested_command": "find . -type f -printf '%s %p\\n' | sort -n | tail -10",
            "alt_commands": [],
        }
    if matched in {
        "all", "list", "count", "every", "all the", "how many",
        "列出", "列举", "所有", "全部", "几个", "多少", "排序",
    }:
        return {
            "reason": f"contains '{matched}' (listing / counting)",
            "suggested_command": "git ls-files | head     # or:  find . -type f | wc -l",
            "alt_commands": [],
        }
    return {
        "reason": f"contains '{matched}'",
        "suggested_command": "git log / find — this looks like a metadata query",
        "alt_commands": [],
    }


def render_out_of_scope_hint(hint: dict, query: str) -> str:
    """Format the hint dict into the multi-line CLI message we print
    before running the (likely-poor) semantic search anyway."""

    lines = [
        f"💡 Heads up: \"{query}\" looks like a metadata query",
        f"   ({hint['reason']}). skygrep is a *content* search tool;",
        f"   the answer you probably want is:",
        f"       {hint['suggested_command']}",
    ]
    for alt in hint.get("alt_commands", [])[:2]:
        lines.append(f"       or: {alt}")
    lines.append("   Running semantic search anyway — set SKYGREP_NO_HINTS=1 to suppress.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# First-run nudge
# ---------------------------------------------------------------------------

# Stored in the metadata table once we've shown the nudge for this
# project, so we don't repeat it on every query. Key is per-project
# implicitly (the metadata table lives in the same DB).
_NUDGE_SHOWN_KEY = "first_run_nudge_shown"


def should_show_first_run_nudge(conn) -> bool:
    """True iff we should show the first-run nudge for this project.

    Two conditions: (a) the metadata flag isn't set yet and (b) the
    chunks table is empty (i.e. this really is the first query ever
    against this index). Defensive against missing tables on
    pre-0.2.2 indexes.
    """

    if os.environ.get("SKYGREP_NO_HINTS") == "1":
        return False
    try:
        existing = conn.execute(
            "SELECT value FROM metadata WHERE key = ?", (_NUDGE_SHOWN_KEY,),
        ).fetchone()
        if existing:
            return False
    except sqlite3.OperationalError:
        # No metadata table yet — pre-0.2.2 index. Caller should still
        # be able to show the nudge once the table gets created.
        pass
    try:
        row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
    except sqlite3.OperationalError:
        return True
    return row is None or row[0] == 0


def mark_first_run_nudge_shown(conn) -> None:
    """Record that the first-run nudge has been shown so we don't
    repeat it on every query in this project."""

    try:
        conn.execute(
            "INSERT INTO metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_NUDGE_SHOWN_KEY, "1"),
        )
        conn.commit()
    except sqlite3.OperationalError:
        return


def render_first_run_nudge() -> str:
    """Three-line greeting shown the first time skygrep runs in a
    fresh project."""

    return (
        "👋 First time in this project — skygrep is auto-indexing in the background.\n"
        "   This first query falls back to rg in <1 s; semantic queries follow as the index builds.\n"
        "   Try `skygrep doctor` for a health check, or `skygrep setup` to register with your LLM CLI."
    )


# ---------------------------------------------------------------------------
# Low-confidence result hint
# ---------------------------------------------------------------------------

# A result is considered low-confidence when both of these hold:
#   - top-1 cosine score is below this floor, AND
#   - the cascade σ-gap was below this floor
# Tuned on the public-OSS bench: 0.30 / 0.005 catches the genuinely
# uncertain queries without false-flagging the merely-medium ones.
LOW_CONF_TOP1_FLOOR = float(os.environ.get("SKYGREP_LOW_CONF_TOP1_FLOOR", "0.30"))
LOW_CONF_SIGMA_FLOOR = float(os.environ.get("SKYGREP_LOW_CONF_SIGMA_FLOOR", "0.005"))


def assess_result_quality(
    results: list[dict] | None,
    cascade_telemetry: dict | None,
) -> Optional[str]:
    """Return a one-line hint when the result set looks
    low-confidence. ``None`` when results are good or quality cannot
    be assessed (no cascade telemetry).

    The floors are tuned on the public-OSS bench: ``0.30`` / ``0.005``
    catches the genuinely uncertain queries (a top-1 cosine of 0.30
    is in the noise band of bge-m3 on a moderately diverse corpus)
    without false-flagging merely-medium ones.
    """

    if os.environ.get("SKYGREP_NO_HINTS") == "1":
        return None
    if not results:
        return (
            "⚠ No results. The index may not include the file you're after — "
            "try `skygrep stats` to see what's indexed, or `skygrep doctor` "
            "for a health check."
        )
    top_score = float(results[0].get("score", 0.0))
    if top_score >= LOW_CONF_TOP1_FLOOR:
        return None
    sigma_gap = (
        float(cascade_telemetry.get("gap", 0.0))
        if cascade_telemetry else 0.0
    )
    if sigma_gap >= LOW_CONF_SIGMA_FLOOR:
        return None
    # Both signals say "uncertain" — surface a recovery menu.
    return (
        "⚠ Top-1 score is low (cosine={:.2f}) and the cascade σ-gap "
        "is below the noise floor. Possible recoveries:\n"
        "       skygrep \"<query>\" --agentic       # decompose into subqueries\n"
        "       skygrep \"<query>\" --top 30        # widen the window\n"
        "       skygrep \"<more specific tokens>\"  # rephrase with code identifiers"
    ).format(top_score)


# ---------------------------------------------------------------------------
# Typo correction
# ---------------------------------------------------------------------------

def closest_match(typed: str, candidates: Iterable[str], *, n: int = 1) -> Optional[str]:
    """Return the closest candidate from ``candidates`` to ``typed``,
    or ``None`` if no candidate is close enough.

    ``difflib.get_close_matches`` uses a default cutoff of 0.6 which
    is right for typo-style errors (one-or-two-character edits)
    without firing on completely-different strings.
    """

    matches = difflib.get_close_matches(typed, list(candidates), n=n, cutoff=0.6)
    return matches[0] if matches else None


def suggest_for_unknown_command(typed: str, known: Iterable[str]) -> Optional[str]:
    """Wrap ``closest_match`` with a CLI-friendly message string."""

    suggested = closest_match(typed, known)
    if suggested is None:
        return None
    return (
        f"Unknown command '{typed}'. Did you mean '{suggested}'?  "
        f"Run `skygrep --help` for the full list."
    )


def suggest_for_unknown_option(typed: str, known: Iterable[str]) -> Optional[str]:
    """Same as :func:`suggest_for_unknown_command` but for flags. The
    typed string is normalised to drop a leading ``--`` so ``--tup``
    matches against ``top`` cleanly."""

    bare = typed.lstrip("-")
    suggested = closest_match(bare, [k.lstrip("-") for k in known])
    if suggested is None:
        return None
    return (
        f"Unknown flag '{typed}'. Did you mean '--{suggested}'?  "
        f"Run `skygrep search --help` for the full list."
    )


# ---------------------------------------------------------------------------
# Helper for the CLI integrator
# ---------------------------------------------------------------------------

def hints_disabled() -> bool:
    """Single source of truth for the master disable flag. Hooked by
    every render path so a user / CI that wants quiet output can set
    ``SKYGREP_NO_HINTS=1`` once and have everything respect it."""

    return os.environ.get("SKYGREP_NO_HINTS") == "1"
