"""LLM-driven query routing — replaces v0.14.0's hand-rolled rules
as the primary source of truth for routing decisions. Hand-rolled
rules survive as a fallback when the LLM is unavailable.

Design contract:

  1. **Intelligence**: a small local LLM (default ``qwen2.5:3b``) reads
     the query and decides what tiers to run, what the most distinctive
     identifier token is, and whether to extract content from binary
     files. No more hand-rolled phrase / token / length rules as
     primary source.
  2. **Accuracy**: the 30 / 30 self-test benchmark is the gate. The
     release pipeline runs it twice (LLM on, LLM off / forced fallback)
     — both must hit 30 / 30. The LLM is never trusted blindly:
     ``confidence < 0.7`` forces ``skip_cascade=False`` so the cascade
     always runs when the model is unsure.
  3. **Speed**: warm LLM call ~50 ms (``OLLAMA_KEEP_ALIVE=-1`` already
     set). Hard timeout 500 ms; on timeout fall back to rule-based
     decision. The result is cached per (query) within the SQLite
     ``meta`` table for the duration of the project session.
  4. **Failure transparency**: every routing decision exposes a
     ``source`` field (``"llm"`` / ``"fallback-rules"`` /
     ``"fallback-mixed"``) and a ``reason`` string. Both surface in
     the CLI telemetry line so the user sees how the query was routed
     and why.

The fallback chain:

  primary    : LLM router (Ollama HTTP, structured JSON output)
                   ↓ on failure
  fallback-1 : ``classify_intent`` from intent.py (v0.14.0 rules)
                   ↓ on failure
  fallback-2 : ``intent="mixed"`` — every tier runs, no smart routing
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import requests

from . import intent as intent_mod
from .config import get_config

logger = logging.getLogger(__name__)


# ---- Tunables ------------------------------------------------------

# How long (seconds) to wait for the LLM to respond before giving up
# and using the rule-based fallback. 0.5 s is generous for a warm
# 3 B model under keep_alive=-1.
LLM_TIMEOUT_SECONDS = float(
    os.environ.get("SKYGREP_LLM_ROUTER_TIMEOUT_SECONDS", "8.0")
)

# Below this confidence the LLM's "skip_cascade" decision is ignored
# — accuracy is the gold standard, never trust an unsure model.
MIN_CONFIDENCE_TO_SKIP_CASCADE = float(
    os.environ.get("SKYGREP_LLM_ROUTER_MIN_CONFIDENCE", "0.7")
)


# ---- Decision dataclass --------------------------------------------


@dataclass
class RouterDecision:
    """Structured routing decision with full provenance."""

    intent: str  # "filename" | "semantic" | "lexical" | "mixed"
    primary_token: str = ""
    skip_cascade: bool = False
    skip_filename: bool = False
    skip_lexical: bool = False
    extract_content: bool = False  # for filename matches on PDF/docx
    confidence: float = 0.0
    source: str = "fallback-mixed"
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # 0.2.6+: scope classification on the same LLM call. Replaces the
    # ``intelligent_cli._METADATA_TOKENS`` keyword list as the PRIMARY
    # detector for "user is asking for filesystem metadata, not content
    # search" — see Principle 1 in ``docs/PRINCIPLES.md`` ("Understanding
    # over Enumeration"). Possible values:
    #   None / ""    → unclassified (older cache entry, rule-based
    #                  fallback, or LLM-unavailable)
    #   "none"       → content / semantic search (the common case)
    #   "recency"    → user wants files by modification time
    #   "size"       → user wants files by size
    #   "listing"    → user wants a flat listing / count of files
    # The keyword list in ``intelligent_cli`` remains as a deterministic
    # offline fallback, but is consulted only when this field is None.
    out_of_scope: str | None = None


def _all_runs() -> RouterDecision:
    """The safest default: run every tier, no skipping. Used as the
    last-resort when both the LLM and the rule-based router fail."""
    return RouterDecision(
        intent="mixed",
        source="fallback-mixed",
        reason="LLM unavailable and rule-based classifier did not produce a confident answer",
    )


# ---- Rule-based fallback (compatibility with v0.14.0) -------------


def _rule_based_decision(query: str) -> RouterDecision:
    """Use the existing v0.14.0 ``classify_intent`` to produce a
    routing decision. Token selection falls through to the v0.14.1
    priority heuristic in ``auto_index.filename_shortcut``, so we
    don't duplicate that logic here."""
    if not query or not query.strip():
        return _all_runs()
    try:
        intent_label = intent_mod.classify_intent(query)
    except Exception as exc:  # noqa: BLE001
        logger.debug("rule-based classify_intent failed: %s", exc)
        return _all_runs()
    return RouterDecision(
        intent=intent_label,
        primary_token="",  # auto_index.filename_shortcut will pick its own
        # Match v0.14.0 semantics: every tier always runs, intent only
        # decides ranking. The LLM router is what enables skip_cascade.
        skip_cascade=False,
        skip_filename=False,
        skip_lexical=False,
        extract_content=False,
        confidence=0.6,
        source="fallback-rules",
        reason=f"rule-based intent={intent_label} (v0.14.0 classifier)",
    )


# ---- LLM call ------------------------------------------------------


_ROUTER_PROMPT = """You are a search-query router for a local code+document search tool.

Given the user's query, decide:
  - intent: one of "filename", "semantic", "lexical", "mixed"
  - primary_token: the single most distinctive identifier token in the
    query that should drive a filename `find -iname '*token*'` match.
    Prefer tokens with digits or unusual capitalisation (e.g. "task-001",
    "v6", "PascalCase") over common English words.
  - skip_cascade: true only when you are CERTAIN the query is a
    filename lookup and semantic content search is unnecessary.
    Default to false; uncertainty MUST keep cascade on.
  - skip_filename: true if the query clearly does not want a filename
    match (descriptive natural-language question).
  - extract_content: true if the user might want PREVIEW content from
    matched files (e.g. PDF / docx), not just metadata.
  - confidence: 0.0 - 1.0. Use < 0.7 when uncertain; anything below
    0.7 will force cascade to run regardless of skip_cascade.
  - reason: one short sentence justifying the decision.
  - out_of_scope: one of "none", "recency", "size", "listing"
    - "recency"  ⇐ user wants files by modification time
                  (e.g. "recent files", "我昨天打开过的", "last week's edits")
    - "size"     ⇐ user wants files by size
                  (e.g. "largest files", "smallest config")
    - "listing"  ⇐ user wants a flat list / count of files
                  (e.g. "list all py files", "how many tests")
    - "none"     ⇐ semantic / lexical / filename content search
                  (the common case — default to this when uncertain)

Output ONLY a JSON object with these exact keys, no prose, no markdown.

Examples:

Query: "where is task-001 file?"
{{"intent": "filename", "primary_token": "task-001", "skip_cascade": true, "skip_filename": false, "extract_content": true, "confidence": 0.95, "reason": "user asks for a specific file by name", "out_of_scope": "none"}}

Query: "how does the auth token get refreshed"
{{"intent": "semantic", "primary_token": "", "skip_cascade": false, "skip_filename": true, "extract_content": false, "confidence": 0.9, "reason": "descriptive question about code behaviour", "out_of_scope": "none"}}

Query: "auth login"
{{"intent": "lexical", "primary_token": "auth", "skip_cascade": false, "skip_filename": false, "extract_content": false, "confidence": 0.7, "reason": "short code-token query, ambiguous", "out_of_scope": "none"}}

Query: "我昨天打开过的十个文件"
{{"intent": "mixed", "primary_token": "", "skip_cascade": false, "skip_filename": false, "extract_content": false, "confidence": 0.9, "reason": "user wants files modified yesterday — filesystem mtime query", "out_of_scope": "recency"}}

Query: "list all the largest python files"
{{"intent": "mixed", "primary_token": "", "skip_cascade": false, "skip_filename": false, "extract_content": false, "confidence": 0.85, "reason": "user wants flat listing of files sorted by size — filesystem query", "out_of_scope": "listing"}}

Now route this query:
Query: "{query}"
"""


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_llm_json(raw: str) -> dict | None:
    """Extract a JSON object from the LLM's raw output. Tolerates
    surrounding prose / markdown the model may emit."""
    if not raw:
        return None
    m = _JSON_BLOCK_RE.search(raw)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _llm_decision(query: str) -> RouterDecision | None:
    """Call the local LLM router. Returns ``None`` on any failure
    (caller falls back). Never raises."""
    cfg = get_config()
    url = f"{cfg['ollama_url']}/api/generate"
    model = os.environ.get(
        "SKYGREP_LLM_ROUTER_MODEL", cfg.get("hyde_model") or cfg["llm_model"]
    )
    prompt = _ROUTER_PROMPT.format(query=query.replace('"', "'"))
    # Coerce ``keep_alive`` to the JSON shape Ollama accepts. The project
    # default ``"-1"`` (string from env) raises HTTP 400 on recent Ollama
    # versions ("time: missing unit in duration -1"); ``_coerce_keep_alive``
    # converts pure-numeric strings to int so the API accepts them.
    from .answerer import _coerce_keep_alive
    keep_alive = _coerce_keep_alive(cfg.get("keep_alive", -1))
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0, "num_predict": 256},
    }
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    try:
        r = requests.post(url, json=payload, timeout=LLM_TIMEOUT_SECONDS)
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError) as exc:
        logger.debug("LLM router HTTP failure: %s", exc)
        return None

    raw = body.get("response", "") if isinstance(body, dict) else ""
    parsed = _parse_llm_json(raw)
    if not parsed:
        logger.debug("LLM router returned non-JSON: %r", raw[:200])
        return None

    intent_val = str(parsed.get("intent", "mixed")).lower()
    if intent_val not in {"filename", "semantic", "lexical", "mixed"}:
        intent_val = "mixed"
    confidence = float(parsed.get("confidence", 0.0) or 0.0)
    skip_cascade = bool(parsed.get("skip_cascade", False))
    # Safety: never trust low-confidence skip_cascade decisions.
    if confidence < MIN_CONFIDENCE_TO_SKIP_CASCADE:
        skip_cascade = False

    # Validate the new 0.2.6 out_of_scope field. The model may omit it
    # (older prompt, weaker model variant, or just hallucinate); we
    # keep the field optional and only accept the four canonical
    # values. Missing / invalid values become None so the caller knows
    # to fall back to the keyword-based detector.
    oos_raw = str(parsed.get("out_of_scope", "") or "").strip().lower()
    if oos_raw in {"none", "recency", "size", "listing"}:
        out_of_scope: str | None = oos_raw
    else:
        out_of_scope = None

    return RouterDecision(
        intent=intent_val,
        primary_token=str(parsed.get("primary_token", "") or "").strip(),
        skip_cascade=skip_cascade,
        skip_filename=bool(parsed.get("skip_filename", False)),
        skip_lexical=bool(parsed.get("skip_lexical", False)),
        extract_content=bool(parsed.get("extract_content", False)),
        confidence=confidence,
        source="llm",
        reason=str(parsed.get("reason", "") or "")[:200],
        raw=parsed,
        out_of_scope=out_of_scope,
    )


# ---- Cache (per session, per project) -----------------------------


def _cache_get(conn: sqlite3.Connection, query: str) -> RouterDecision | None:
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS router_cache "
            "(query TEXT PRIMARY KEY, decision TEXT)"
        )
        row = conn.execute(
            "SELECT decision FROM router_cache WHERE query = ?", (query,)
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if not row or not row[0]:
        return None
    try:
        d = json.loads(row[0])
    except json.JSONDecodeError:
        return None
    # Tolerate cached payloads written by older versions that didn't
    # know about the 0.2.6 ``out_of_scope`` field. ``RouterDecision``
    # has a default for it, so dropping it from ``d`` lets the dataclass
    # apply the default. Conversely, future fields would land in ``d``
    # and ``RouterDecision(**d)`` would raise — so we only pass through
    # the keys we currently know about.
    known = {
        "intent", "primary_token", "skip_cascade", "skip_filename",
        "skip_lexical", "extract_content", "confidence", "source",
        "reason", "raw", "out_of_scope",
    }
    cleaned = {k: v for k, v in d.items() if k in known}
    return RouterDecision(**cleaned)


def _cache_set(conn: sqlite3.Connection, query: str, decision: RouterDecision) -> None:
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS router_cache "
            "(query TEXT PRIMARY KEY, decision TEXT)"
        )
        payload = json.dumps({
            "intent": decision.intent,
            "primary_token": decision.primary_token,
            "skip_cascade": decision.skip_cascade,
            "skip_filename": decision.skip_filename,
            "skip_lexical": decision.skip_lexical,
            "extract_content": decision.extract_content,
            "confidence": decision.confidence,
            "source": decision.source,
            "reason": decision.reason,
            "raw": decision.raw,
            "out_of_scope": decision.out_of_scope,
        })
        conn.execute(
            "INSERT OR REPLACE INTO router_cache (query, decision) "
            "VALUES (?, ?)",
            (query, payload),
        )
        conn.commit()
    except sqlite3.OperationalError:
        pass


# ---- Public API ----------------------------------------------------


# ---- 0.5.0 lazy-index entry-point inference -----------------------
# `infer_candidate_paths()` asks the same local LLM (qwen2.5:3b) to pick
# 5-15 most-likely-relevant paths for the query, given JUST the directory
# tree summary (no embedding, no upfront index). This is what powers the
# user's "find the most likely node from the query alone" vision —
# replacing upfront global indexing with per-query LLM judgement.
#
# The method is content-agnostic — it works on file paths and dir
# structure alone, no language- or framework-specific heuristics.

_CANDIDATE_PATHS_PROMPT = """You are a code-search routing assistant.

Given a user's natural-language question and a project's directory
structure, output the 5-12 most likely DIRECTORIES (not specific
files) where the answer probably lives. Order by likelihood.

Question: "{query}"

Directory structure (each line is one real directory in this project,
followed by its file count):
{tree}

Rules:
- Output ONE directory PATH per line, no other text, no markdown.
- COPY paths VERBATIM from the structure above. Do NOT invent
  subpaths or specific filenames — those are not in the tree.
- Choose only directories that ACTUALLY APPEAR in the list above.
- Prefer leaf-most matching directories (e.g. ``django/db/migrations``
  over ``django/db``).
- Output at most 12 directories. Stop when no more directory feels
  relevant.

Directories:
"""


_QUESTION_PREFIX_RE = re.compile(
    r"^\s*(where|how|what|which|when|why|does|do|is|are|can|could|"
    r"would|should|will|may|might)\s+",
    re.IGNORECASE,
)
_FILLER_TOKENS_RE = re.compile(
    r"\b(the|a|an|that|this|those|these|some|any|all|every|"
    r"such)\b\s*",
    re.IGNORECASE,
)


def simplify_router_query(query: str) -> str:
    """Strip English question-words and common fillers so the
    LLM router prompt sees just the content nouns / verbs.

    The 0.5.3 bench showed qwen2.5:3b is highly sensitive to
    phrasing: ``"where does Django apply migrations"`` produces
    accurate dir picks, while the verbose oracle wording
    ``"Where is the migration runner that applies pending schema
    changes to the database?"`` drives qwen toward unrelated
    directories (``tests/gis_tests``, ``docs/releases``, …).

    This is a deterministic, language-agnostic strip. It removes:
    - Leading question words (``where``/``how``/``does``/…)
    - Common fillers (``the``/``a``/``that``/…)
    - Trailing punctuation
    - Repeated whitespace

    Never raises; always returns a non-empty string (falls back
    to the original query if simplification yields blank).
    """
    if not query.strip():
        return query
    s = query.strip().rstrip("?.!").strip()
    # Strip leading question word(s) — "Where is" / "How does"
    # may chain a couple ("Where does the X that Y").
    for _ in range(3):
        new_s = _QUESTION_PREFIX_RE.sub("", s, count=1).strip()
        if new_s == s:
            break
        s = new_s
    s = _FILLER_TOKENS_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if s else query


def infer_candidate_paths(
    query: str, tree_summary: str, *, max_paths: int = 15,
    timeout: float = 8.0,
) -> list[str]:
    """Ask the local LLM to pick likely paths from the dir tree alone.

    Returns a list of paths (relative or absolute, as the LLM emits).
    Returns an empty list on any failure (caller falls back to
    deterministic token-shortcut). Never raises.

    0.5.3 retry-with-simplified-query: if the verbatim query yields
    fewer than 5 picks (qwen2.5:3b sometimes fixates on filler words
    in long natural-language phrasings), re-issue with
    ``simplify_router_query`` applied. The second pass adds 1–4 s to
    cold-start latency in the worst case but lifts hit-rate
    materially on the Django oracle bench.

    NOTE on timeout: the module-level ``LLM_TIMEOUT_SECONDS`` is 0.5 s
    — appropriate for the routing JSON call (single short verdict
    line), too tight for this function which generates a 384-token
    path list. 0.5.3 raised the default here to 8 s, which is well
    inside the cold-start lazy budget (≤ 10 s) and gives qwen2.5:3b
    enough time to emit 5–15 paths even on a 5000-file repo.
    """
    if not query.strip() or not tree_summary.strip():
        return []
    cfg = get_config()
    url = f"{cfg['ollama_url']}/api/generate"
    model = os.environ.get(
        "SKYGREP_LLM_ROUTER_MODEL", cfg.get("hyde_model") or cfg["llm_model"]
    )

    def _call_one(q: str) -> list[str]:
        payload = {
            "model": model,
            "prompt": _CANDIDATE_PATHS_PROMPT.format(
                query=q.replace('"', "'"),
                tree=tree_summary[:4000],
            ),
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 384},
        }
        # 0.5.3: omit ``keep_alive`` — the project default is the
        # *string* ``"-1"`` which Ollama's ``/api/generate`` parses
        # as a duration (``time: missing unit in duration "-1"``)
        # and rejects with HTTP 400, silently zeroing this
        # function's output. The config-level sentinel is meant for
        # the embedding endpoint which accepts duration strings;
        # the generate endpoint expects integer seconds OR a
        # unit-suffixed string. Omitting the field falls back to
        # Ollama's default 5 m, which is fine for a small qwen
        # model.
        k = cfg.get("keep_alive")
        if isinstance(k, int):
            payload["keep_alive"] = k
        try:
            r = requests.post(url, json=payload, timeout=timeout)
            r.raise_for_status()
            raw = (r.json() or {}).get("response", "")
        except (requests.RequestException, ValueError):
            return []
        out: list[str] = []
        for line in raw.splitlines():
            line = line.strip().lstrip("-•* ").strip().rstrip(",")
            if not line or len(line) > 250:
                continue
            if line.endswith((".", "!", "?")):
                continue
            if line.lower().startswith(("paths", "answer", "the ", "based ")):
                continue
            out.append(line)
            if len(out) >= max_paths:
                break
        return out

    picks = _call_one(query)
    # 0.5.3: if the verbatim call yielded few picks, retry with a
    # simplified query (question-words and fillers stripped). qwen
    # 2.5:3b is highly sensitive to phrasing — long oracle wordings
    # like "Where is the X that Y" mislead it; the bare topic
    # ("X Y") tends to land much closer to the right directories.
    simplified = simplify_router_query(query)
    if len(picks) < max_paths and simplified and simplified != query:
        retry = _call_one(simplified)
        seen: set[str] = set(picks)
        for p in retry:
            if p not in seen:
                picks.append(p)
                seen.add(p)
                if len(picks) >= max_paths:
                    break
    return picks


def route_query(
    query: str,
    *,
    conn: sqlite3.Connection | None = None,
    use_llm: bool = True,
) -> RouterDecision:
    """Resolve a query into a structured ``RouterDecision``.

    Three-layer fallback chain:

      1. SQLite per-session cache (skip if no ``conn``).
      2. LLM router via Ollama HTTP (skip if ``use_llm=False``).
      3. Rule-based ``classify_intent`` from v0.14.0.
      4. Final safe default: ``intent="mixed"`` — every tier runs.

    Never raises — returns a valid ``RouterDecision`` even on
    arbitrary failure. Failure mode is exposed via the ``source``
    and ``reason`` fields.
    """
    if not query or not query.strip():
        return _all_runs()

    if conn is not None:
        cached = _cache_get(conn, query)
        if cached is not None:
            return cached

    if use_llm:
        try:
            decision = _llm_decision(query)
        except Exception as exc:  # noqa: BLE001 — never let routing crash a search
            logger.debug("LLM router unexpected error: %s", exc)
            decision = None
        if decision is not None:
            if conn is not None:
                _cache_set(conn, query, decision)
            return decision

    decision = _rule_based_decision(query)
    if conn is not None:
        _cache_set(conn, query, decision)
    return decision
