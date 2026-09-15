# SPDX-License-Identifier: Apache-2.0
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
  3. **Speed**: a local fast-intent substrate handles obvious filename /
     semantic cases without an LLM call. Ambiguous cases fall through to
     the small local LLM with a hard timeout; any failure falls back to
     safe rules.
  4. **Failure transparency**: every routing decision exposes a
     ``source`` field (``"fast-intent"`` / ``"llm"`` /
     ``"fallback-rules"`` / ``"fallback-mixed"``) and a ``reason`` string.
     Both surface in
     the CLI telemetry line so the user sees how the query was routed
     and why.

The fallback chain:

  primary    : fast intent substrate (local n-gram classifier)
                   ↓ on uncertainty
  fallback-0 : LLM router (Ollama HTTP, structured JSON output)
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
from .fast_intent import best_filename_token, classify_fast_intent
from .metadata_search import MetadataFacet, analyze_metadata_query
from .query_scope import strip_scope_clauses

logger = logging.getLogger(__name__)


# ---- Tunables ------------------------------------------------------

# How long (seconds) to wait for the LLM to respond before giving up
# and using the rule-based fallback. 0.5 s is generous for a warm
# 3 B model under keep_alive=-1.
LLM_TIMEOUT_SECONDS = float(
    os.environ.get("SKYGREP_LLM_ROUTER_TIMEOUT_SECONDS", "8.0")
)

ROUTER_CACHE_VERSION = 4

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
    # Metadata is a facet, not an intent. ``metadata_terminal`` means the
    # query can be answered entirely from filesystem metadata; otherwise the
    # facet is only a ranking/filter signal and semantic depth must continue.
    metadata_kind: str | None = None
    metadata_terminal: bool = False


def _all_runs() -> RouterDecision:
    """The safest default: run every tier, no skipping. Used as the
    last-resort when both the LLM and the rule-based router fail."""
    return RouterDecision(
        intent="mixed",
        source="fallback-mixed",
        reason="LLM unavailable and rule-based classifier did not produce a confident answer",
    )


def _attach_metadata_facet(
    decision: RouterDecision,
    facet: MetadataFacet | None,
) -> RouterDecision:
    """Attach filesystem metadata as a plan facet, never as an intent."""

    if facet is None:
        return decision
    decision.metadata_kind = facet.kind
    decision.metadata_terminal = facet.terminal
    if not facet.terminal and decision.out_of_scope in {"recency", "size", "listing"}:
        decision.out_of_scope = "none"
    if facet.terminal:
        decision.out_of_scope = "size" if facet.kind == "size" else "recency"
    raw = dict(decision.raw or {})
    raw["metadata_facet"] = {
        "kind": facet.kind,
        "terminal": facet.terminal,
        "descriptor_count": len(facet.target_descriptors),
    }
    decision.raw = raw
    if not facet.terminal and "metadata facet" not in (decision.reason or ""):
        suffix = f"metadata facet={facet.kind} used as modifier, not terminal"
        decision.reason = (f"{decision.reason}; {suffix}" if decision.reason else suffix)[:200]
    return decision


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
    For filename lookups in any language, output the smallest distinctive
    substring likely to appear in the basename, NOT the whole natural-language
    phrase. Remove surrounding function words generically; keep the actual
    filename clue.
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

Important: recency / size words are metadata constraints, not always the
whole answer. If the query asks for a particular target or content ("the report
I recently created", "latest python file that handles auth"), keep
out_of_scope="none" and keep semantic / filename search enabled.

Output ONLY a JSON object with these exact keys, no prose, no markdown.

Examples:

Query: "where is task-001 file?"
{{"intent": "filename", "primary_token": "task-001", "skip_cascade": true, "skip_filename": false, "extract_content": true, "confidence": 0.95, "reason": "user asks for a specific file by name", "out_of_scope": "none"}}

Query: "我的 CASE42 文件在哪"
{{"intent": "filename", "primary_token": "CASE42", "skip_cascade": true, "skip_filename": false, "extract_content": true, "confidence": 0.9, "reason": "user asks for a file by a distinctive filename token", "out_of_scope": "none"}}

Query: "我的合同文件在哪"
{{"intent": "filename", "primary_token": "合同", "skip_cascade": true, "skip_filename": false, "extract_content": true, "confidence": 0.85, "reason": "user asks for a file by a distinctive filename term", "out_of_scope": "none"}}

Query: "how does the auth token get refreshed"
{{"intent": "semantic", "primary_token": "", "skip_cascade": false, "skip_filename": true, "extract_content": false, "confidence": 0.9, "reason": "descriptive question about code behaviour", "out_of_scope": "none"}}

Query: "auth login"
{{"intent": "lexical", "primary_token": "auth", "skip_cascade": false, "skip_filename": false, "extract_content": false, "confidence": 0.7, "reason": "short code-token query, ambiguous", "out_of_scope": "none"}}

Query: "我昨天打开过的十个文件"
{{"intent": "mixed", "primary_token": "", "skip_cascade": false, "skip_filename": false, "extract_content": false, "confidence": 0.9, "reason": "user wants files modified yesterday — filesystem mtime query", "out_of_scope": "recency"}}

Query: "where is the report I recently created in PROJECT"
{{"intent": "filename", "primary_token": "", "skip_cascade": false, "skip_filename": false, "extract_content": true, "confidence": 0.75, "reason": "recency constrains a specific artifact search rather than answering alone", "out_of_scope": "none"}}

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


def _llm_decision(
    query: str,
    *,
    timeout: float | None = None,
) -> RouterDecision | None:
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
        r = requests.post(
            url,
            json=payload,
            timeout=timeout if timeout is not None else LLM_TIMEOUT_SECONDS,
        )
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

    primary_token = str(parsed.get("primary_token", "") or "").strip()
    anchor_token = best_filename_token(query)
    skip_filename = bool(parsed.get("skip_filename", False))
    if anchor_token and intent_val in {"semantic", "mixed"}:
        # A filename-like token inside a semantic question is an anchor,
        # not a reason to suppress semantic search. Keep the filename tier
        # available so retrieval can locate the concrete file first, while
        # skip_cascade remains false unless the router explicitly proved a
        # path-only request.
        skip_filename = False
        if not primary_token:
            primary_token = anchor_token

    return RouterDecision(
        intent=intent_val,
        primary_token=primary_token,
        skip_cascade=skip_cascade,
        skip_filename=skip_filename,
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
    if d.get("cache_version") != ROUTER_CACHE_VERSION:
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
        "reason", "raw", "out_of_scope", "metadata_kind",
        "metadata_terminal",
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
            "cache_version": ROUTER_CACHE_VERSION,
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
            "metadata_kind": decision.metadata_kind,
            "metadata_terminal": decision.metadata_terminal,
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
_TRAILING_LOCATION_PREDICATE_RE = re.compile(
    r"\b(live|lives|located|implemented|defined|declared|stored|kept)\s*$",
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
    # Location questions often end with a predicate shell ("where does X
    # live?", "where is X implemented?"). For retrieval, that shell is not
    # evidence; the content phrase before it is.
    s = _TRAILING_LOCATION_PREDICATE_RE.sub("", s).strip()
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
    timeout: float | None = None,
) -> RouterDecision:
    """Resolve a query into a structured ``RouterDecision``.

    Fallback chain:

      1. Fast local intent substrate for obvious filename/semantic queries.
      2. SQLite per-session cache (skip if no ``conn``).
      3. LLM router via Ollama HTTP (skip if ``use_llm=False``).
      4. Rule-based ``classify_intent`` from v0.14.0.
      5. Final safe default: ``intent="mixed"`` — every tier runs.

    Never raises — returns a valid ``RouterDecision`` even on
    arbitrary failure. Failure mode is exposed via the ``source``
    and ``reason`` fields.
    """
    if not query or not query.strip():
        return _all_runs()

    routing_query = strip_scope_clauses(query).strip() or query
    metadata_facet = analyze_metadata_query(query)
    if metadata_facet is not None and metadata_facet.terminal:
        oos = "size" if metadata_facet.kind == "size" else "recency"
        return RouterDecision(
            intent="mixed",
            primary_token="",
            skip_cascade=False,
            skip_filename=False,
            skip_lexical=False,
            extract_content=False,
            confidence=0.95,
            source="fast-metadata",
            reason=metadata_facet.reason,
            out_of_scope=oos,
            metadata_kind=metadata_facet.kind,
            metadata_terminal=True,
        )

    fast = classify_fast_intent(routing_query)
    # Metadata is a terminal fast lane only when classify_metadata_query()
    # accepted the whole query above. If fast-intent sees recency/size words
    # inside a query with residual target constraints, those words are search
    # modifiers, not the answer. Fall through to LLM / safe routing so content
    # depth is still decided by the full query.
    if fast is not None and fast.intent == "metadata":
        cheap_intent = "mixed" if metadata_facet is not None else "semantic"
        cheap = RouterDecision(
            intent=cheap_intent,
            primary_token="",
            skip_cascade=False,
            skip_filename=False,
            skip_lexical=False,
            extract_content=False,
            confidence=max(0.65, min(0.85, fast.confidence - 0.1)),
            source="fast-intent",
            reason=(
                "fast metadata signal treated as a modifier; "
                "continuing content retrieval without an LLM call"
            ),
            out_of_scope="none",
        )
        cheap = _attach_metadata_facet(cheap, metadata_facet)
        if conn is not None:
            _cache_set(conn, routing_query, cheap)
        return cheap
    if fast is not None and fast.intent != "metadata":
        anchor_token = best_filename_token(routing_query)
        primary_token = fast.primary_token
        skip_filename = fast.intent == "semantic"
        if fast.intent == "semantic" and anchor_token:
            primary_token = anchor_token
            skip_filename = False
        cheap = RouterDecision(
            intent=fast.intent,
            primary_token=primary_token,
            # The fast substrate is allowed to pick the policy lane, but not
            # to suppress comprehensive retrieval by itself. The CLI still
            # returns immediately when filename evidence is actually found.
            skip_cascade=False,
            skip_filename=skip_filename,
            skip_lexical=False,
            extract_content=(fast.intent == "filename"),
            confidence=fast.confidence,
            source="fast-intent",
            reason=fast.reason,
            out_of_scope="none",
        )
        cheap = _attach_metadata_facet(cheap, metadata_facet)
        if conn is not None:
            _cache_set(conn, routing_query, cheap)
        return cheap

    cached: RouterDecision | None = None
    if conn is not None:
        cached = _cache_get(conn, routing_query)
        if cached is not None:
            # 0.5.8.1: stale-cache invalidation. If a previous version cached
            # a ``fallback-*`` decision (rule-based or all-runs), 0.5.7's
            # silent HTTP 400 / aggressive timeout meant essentially every
            # uncached query would land here and be cached as fallback —
            # 0.5.8 fixed both bugs but cached entries from 0.5.7 still
            # poison the cache. When ``use_llm=True``, treat ``fallback-*``
            # cache hits as a miss and let the LLM call run; the cache
            # entry is rewritten below if the LLM succeeds, otherwise the
            # original fallback is returned at the end.
            cached_src = (cached.source or "")
            if not (use_llm and cached_src.startswith("fallback")):
                return cached

    if use_llm:
        try:
            decision = _llm_decision(routing_query, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 — never let routing crash a search
            logger.debug("LLM router unexpected error: %s", exc)
            decision = None
        if decision is not None:
            decision = _attach_metadata_facet(decision, metadata_facet)
            if conn is not None:
                _cache_set(conn, routing_query, decision)
            return decision

    # If we passed through a stale ``fallback-*`` cache hit and the LLM
    # call failed, return the cached fallback rather than re-running the
    # rule-based classifier (we already have the answer).
    if cached is not None:
        return _attach_metadata_facet(cached, metadata_facet)

    decision = _rule_based_decision(routing_query)
    decision = _attach_metadata_facet(decision, metadata_facet)
    if conn is not None:
        _cache_set(conn, routing_query, decision)
    return decision
