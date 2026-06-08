"""Recall-safe candidate path substrate.

The router should understand intent, but it should not be the only gate that
decides which files are allowed into retrieval. This module builds a cheap,
content-agnostic candidate path pool from independent signals, then lets the
semantic scorer/reranker decide which candidates deserve snippet evidence.
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from .hybrid import extract_query_terms
from .storage import lexical_score, path_matches

logger = logging.getLogger(__name__)

_SYMBOL_HEADER_RE = re.compile(r"\[symbol:\s*([^\]]+)\]")
_IDENTIFIER_RE = re.compile(
    r"^\s*(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)|"
    r"^\s*([A-Z][A-Z0-9_]{2,})\s*=",
    re.MULTILINE,
)
_TEST_INTENT_TERMS = {
    "test",
    "tests",
    "testing",
    "covers",
    "cover",
    "covered",
    "proves",
    "prove",
    "assert",
    "asserts",
    "regression",
    "fixture",
    "fixtures",
}
_LOCATION_INTENT_TERMS = {
    "where",
    "which",
    "file",
    "path",
    "locate",
    "find",
    "implementation",
    "implemented",
    "wired",
}
_CODE_SEARCH_SYNONYMS = {
    "skipped": ("skip", "ignore", "ignored"),
    "skip": ("ignore", "ignored"),
    "ignored": ("ignore", "skip", "skipped"),
    "directories": ("directory", "dirs", "dir"),
    "directory": ("directories", "dirs", "dir"),
    "database": ("db",),
    "environment": ("env",),
    "variables": ("vars",),
    "variable": ("var",),
    "duplicate": ("dedupe", "dedup", "unique", "seen"),
    "duplicates": ("dedupe", "dedup", "unique", "seen"),
    "logical": ("key", "identity"),
}


def classify_agent_query_intent(query: str) -> str:
    """Classify the retrieval job, not the user's whole natural-language intent.

    The generic router's ``semantic`` / ``filename`` labels are too coarse for
    agent context. "Which test covers X?" should rank test artifacts first,
    while "where is X implemented?" should favor source files and treat docs as
    supporting evidence. This small classifier only drives ranking priors; it
    never excludes lanes.
    """

    q = query.lower()
    tokens = set(re.findall(r"[a-z0-9_]+", q))
    if tokens & _TEST_INTENT_TERMS:
        return "test_location"
    if (
        {"environment", "variables"} <= tokens
        or "env" in tokens
        or ({"database", "ollama"} <= tokens)
    ):
        return "config_location"
    if (
        tokens & {"vendor", "generated", "directories", "directory", "ignored"}
        and tokens & {"default", "skip", "skipped", "ignore", "ignored"}
    ):
        return "indexing_rules"
    if (
        {"search", "results"} <= tokens
        and tokens & {"duplicate", "duplicates", "logical", "skipped", "dedupe", "dedup"}
    ):
        return "search_result_logic"
    if ("where is" in q or "which file" in q or "locate " in q
            or tokens & _LOCATION_INTENT_TERMS):
        return "file_location"
    if any(term in tokens for term in ("implement", "implemented", "wired", "provider")):
        return "implementation"
    return "semantic"


def source_type(path: str) -> str:
    path_lc = path.replace("\\", "/").lower()
    name = Path(path_lc).name
    if (
        "/tests/" in path_lc
        or path_lc.startswith("tests/")
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name.endswith("_test.rs")
        or name.endswith(".test.ts")
        or name.endswith(".spec.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.tsx")
    ):
        return "test"
    if (
        path_lc.startswith("docs/")
        or "/docs/" in path_lc
        or path_lc.endswith((".md", ".html", ".rst"))
    ):
        return "doc"
    if name in {"cargo.lock", "package-lock.json", "pnpm-lock.yaml", "uv.lock"}:
        return "lockfile"
    if any(part in path_lc for part in ("/node_modules/", "/dist/", "/build/", "/target/")):
        return "generated"
    return "source"


def _source_type_prior(intent: str, path: str) -> float:
    stype = source_type(path)
    path_lc = path.replace("\\", "/").lower()
    basename = Path(path_lc).name
    if intent == "test_location":
        if stype == "test":
            return 2.50
        if stype == "source":
            return 0.15
        if stype == "doc":
            return -1.50
        return -1.00
    if intent == "config_location":
        if basename.startswith("config.") or basename in {"config.py", "config.rs"}:
            return 2.40
        if "/config/" in path_lc or path_lc.endswith("/config"):
            return 1.30
        if stype == "doc":
            return -0.45
        if stype == "test":
            return -0.80
        return 0.10
    if intent == "indexing_rules":
        if basename in {"indexer.py", "indexer.rs"} or basename.endswith("_indexer.py"):
            return 1.90
        if "ignore" in basename:
            return 1.00
        if stype == "test":
            return -1.20
        if stype == "doc":
            return -0.40
        return 0.10
    if intent == "search_result_logic":
        if stype == "test":
            return -5.00
        if basename in {"storage.py", "search.rs"}:
            return 1.60
        if "search" in basename or "storage" in basename:
            return 1.00
        if stype == "doc":
            return -0.40
        return 0.10
    if intent in {"file_location", "implementation"}:
        if stype == "source":
            return 0.35
        if stype == "test":
            return -0.90
        if stype == "doc":
            return -0.35
        return -0.80
    if stype in {"lockfile", "generated"}:
        return -0.80
    return 0.0


def _norm_token(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _norm_forms(value: str) -> set[str]:
    norm = _norm_token(value)
    if not norm:
        return set()
    forms = {norm}
    if len(norm) > 4 and norm.endswith("s"):
        forms.add(norm[:-1])
    if len(norm) > 6 and norm.endswith("ing"):
        stem = norm[:-3]
        forms.add(stem)
        if len(stem) > 2 and stem[-1] == stem[-2]:
            forms.add(stem[:-1])
        forms.add(stem + "e")
    if len(norm) > 5 and norm.endswith("ed"):
        stem = norm[:-2]
        forms.add(stem)
        forms.add(stem + "e")
    return {form for form in forms if form}


def _surface_variants(value: str) -> list[str]:
    variants = [value.lower()]
    for form in _norm_forms(value):
        if len(form) >= 4 and form not in variants:
            variants.append(form)
    for synonym in _CODE_SEARCH_SYNONYMS.get(value.lower(), ()):
        if synonym not in variants:
            variants.append(synonym)
    return variants


def _matches_filters(
    path: str,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
) -> bool:
    return path_matches(path, include_patterns, exclude_patterns)


def _identifier_names(chunk: str) -> list[str]:
    names: list[str] = []
    for match in _SYMBOL_HEADER_RE.finditer(chunk):
        value = match.group(1).strip()
        if value:
            names.append(value)
    # Keep this bounded to the first few lines so a term in an arbitrary body
    # paragraph does not masquerade as an identifier anchor.
    head = "\n".join(chunk.splitlines()[:12])
    for match in _IDENTIFIER_RE.finditer(head):
        value = (match.group(1) or match.group(2) or "").strip()
        if value:
            names.append(value)
    return names


def _identity_score(terms: list[str], chunk: str) -> float:
    if not terms:
        return 0.0
    names = _identifier_names(chunk)
    if not names:
        return 0.0
    score = 0.0
    term_forms = [_norm_forms(term) for term in terms if len(term) > 2]
    for name in names:
        norm_name = _norm_token(name)
        if not norm_name:
            continue
        name_bonus = 0.0
        if name.isupper():
            name_bonus += 0.35
        elif not name.startswith("_"):
            name_bonus += 0.20
        for forms in term_forms:
            if not forms:
                continue
            if norm_name in forms:
                score += 0.70 + name_bonus
            elif any(form in norm_name for form in forms):
                score += 0.45 + name_bonus
    return min(score, 1.5)


def _identifier_term_hits(terms: list[str], names: list[str]) -> set[str]:
    hits: set[str] = set()
    term_forms = [_norm_forms(term) for term in terms if len(term) > 2]
    for name in names:
        norm_name = _norm_token(name)
        if not norm_name:
            continue
        for forms in term_forms:
            matched = next((form for form in forms if form in norm_name), "")
            if matched:
                hits.add(matched)
    return hits


def _identifier_term_order(terms: list[str], hits: set[str]) -> int:
    for index, term in enumerate(terms):
        if hits.intersection(_norm_forms(term)):
            return index
    return 999


def _symbol_anchor_map(
    conn: sqlite3.Connection,
    candidate_paths: set[str],
    terms: list[str],
) -> dict[str, list[dict[str, Any]]]:
    if not candidate_paths or not terms:
        return {}
    placeholders = ",".join("?" * len(candidate_paths))
    try:
        rows = conn.execute(
            f"""
            SELECT file, name, kind, start_line, end_line
            FROM symbols
            WHERE file IN ({placeholders})
            """,
            sorted(candidate_paths),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    term_forms = [
        {_norm_token(variant) for variant in _surface_variants(term) if _norm_token(variant)}
        for term in terms
        if len(term) > 2
    ]
    anchors: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for file, name, kind, start, end in rows:
        norm_name = _norm_token(str(name))
        if not norm_name:
            continue
        hits = {
            form
            for forms in term_forms
            for form in forms
            if form and form in norm_name
        }
        if not hits:
            continue
        anchors[str(file)].append(
            {
                "name": str(name),
                "kind": str(kind or ""),
                "start_line": int(start or 0),
                "end_line": int(end or start or 0),
                "hits": sorted(hits),
            }
        )
    return anchors


def _symbol_anchor_score(
    anchors: dict[str, list[dict[str, Any]]],
    path: str,
    start_line: int | None,
    end_line: int | None,
) -> tuple[float, list[str]]:
    if not start_line or not end_line:
        return 0.0, []
    score = 0.0
    names: list[str] = []
    for anchor in anchors.get(path, []):
        a_start = int(anchor.get("start_line") or 0)
        a_end = int(anchor.get("end_line") or a_start)
        if a_start and a_end and not (end_line < a_start or start_line > a_end):
            hits = anchor.get("hits") or []
            score = max(score, 0.45 + (0.15 * len(hits)))
            names.append(str(anchor.get("name") or ""))
    return score, names


def _is_constant_anchor(item: dict) -> bool:
    return any(str(name).isupper() for name in item.get("identifier_names", []))


def _is_public_symbol_anchor(item: dict) -> bool:
    for name in item.get("identifier_names", []):
        text = str(name)
        if text and not text.startswith("_") and not text.isupper():
            return True
    return False


def _anchor_sort_key(item: dict) -> tuple:
    names = [str(name) for name in item.get("identifier_names", [])]
    joined = " ".join(names).lower()
    helper_penalty = 1 if "test" in joined or "fixture" in joined else 0
    return (
        -len(item.get("identifier_term_hits", set())),
        -float(item.get("identity_score") or 0.0),
        int(item.get("identifier_term_order", 999)),
        helper_penalty,
        int(item.get("start_line") or 0),
    )


def _covered_terms(terms: list[str], chunk: str) -> set[str]:
    lowered = chunk.lower()
    normalized = _norm_token(chunk)
    covered: set[str] = set()
    for term in terms:
        forms = _norm_forms(term)
        if not forms:
            continue
        matched = next(
            (
                form
                for form in forms
                if form in normalized or form in lowered
            ),
            "",
        )
        if matched:
            covered.add(matched)
    return covered


def _covered_query_terms(query: str, text: str, *, max_terms: int = 12) -> list[str]:
    terms = extract_query_terms(query, max_terms=max_terms)
    covered = _covered_terms(terms, text)
    ordered: list[str] = []
    for term in terms:
        if covered.intersection(_norm_forms(term)):
            ordered.append(term)
    return ordered


def _support_block(item: dict, *, max_chars: int = 600) -> str:
    start = item.get("start_line")
    end = item.get("end_line")
    if start is not None and end is not None:
        loc = f"{item.get('path', item.get('file', ''))}:{start}-{end}"
    else:
        loc = str(item.get("path") or item.get("file") or "")
    text = str(item.get("snippet") or item.get("chunk") or "").strip()
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return f"[related evidence: {loc}]\n{text}"


def _attach_supporting_chunks(
    best: dict[str, dict],
    scored: list[dict],
    terms: list[str],
    *,
    per_path: int = 4,
) -> None:
    """Attach a bounded evidence pack to each chosen file result.

    The primary chunk answers "what is the best snippet in this file?" For
    agent calls, that is often not enough: the same file may also need a
    symbol definition, module-level constant, or test assertion anchor. This
    adds a tiny per-file support pack from the same recalled candidates, so
    downstream LLMs get enough context without widening to raw tree dumps.
    """

    if not best or per_path <= 0:
        return
    by_path: dict[str, list[dict]] = defaultdict(list)
    for item in scored:
        by_path[item["path"]].append(item)

    for path, primary in best.items():
        primary_id = primary.get("id")
        covered = _covered_terms(terms, str(primary.get("snippet") or ""))
        support: list[dict] = []
        used_ids = {primary_id}
        anchors = [
            item
            for item in by_path.get(path, [])
            if item.get("id") not in used_ids
            and float(item.get("identity_score") or 0.0) >= 0.35
        ]
        constants = sorted(
            (item for item in anchors if _is_constant_anchor(item)),
            key=_anchor_sort_key,
        )
        publics = sorted(
            (item for item in anchors if _is_public_symbol_anchor(item)),
            key=_anchor_sort_key,
        )
        for group in (constants, publics):
            if len(support) >= per_path:
                break
            for item in group:
                item_id = item.get("id")
                if item_id in used_ids:
                    continue
                support.append(item)
                used_ids.add(item_id)
                covered.update(_covered_terms(terms, str(item.get("snippet") or "")))
                break

        candidates = []
        for item in by_path.get(path, []):
            if item.get("id") in used_ids:
                continue
            chunk = str(item.get("snippet") or item.get("chunk") or "")
            item_terms = _covered_terms(terms, chunk)
            adds_terms = bool(item_terms - covered)
            identity = float(item.get("identity_score") or 0.0)
            chunk_lex = float(item.get("chunk_lexical_score") or 0.0)
            if not adds_terms and identity < 0.35 and chunk_lex < 0.25:
                continue
            support_score = (2.0 * identity) + chunk_lex + (0.2 * len(item_terms))
            candidates.append((support_score, adds_terms, item_terms, item))
        candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
        for _, _, item_terms, item in candidates:
            support.append(item)
            used_ids.add(item.get("id"))
            covered.update(item_terms)
            if len(support) >= per_path:
                break
        if not support:
            continue
        support_payload = "\n\n".join(_support_block(item) for item in support)
        combined = f"{primary.get('snippet', primary.get('chunk', '')).rstrip()}\n\n{support_payload}"
        primary["snippet"] = combined
        primary["chunk"] = combined
        primary["supporting_chunks"] = [
            {
                "path": item.get("path"),
                "start_line": item.get("start_line"),
                "end_line": item.get("end_line"),
                "score": round(float(item.get("score") or 0.0), 4),
            }
            for item in support
        ]


def _confidence_for_results(
    query: str,
    results: list[dict],
    telemetry: dict[str, Any],
) -> dict[str, Any]:
    if not results:
        return {
            "quality": "uncertain",
            "confidence": 0.0,
            "missing_signal": "no candidate evidence survived ranking",
            "suggested_followup_probe": f"rg -n --hidden --glob '!node_modules/**' -- {query!r}",
        }
    terms = extract_query_terms(query, max_terms=12)
    top = results[0]
    text = f"{top.get('path', '')}\n{top.get('snippet') or top.get('chunk') or ''}"
    covered = _covered_query_terms(query, text, max_terms=12)
    coverage = len(covered) / max(1, min(len(terms), 8))
    top_score = float(top.get("score") or 0.0)
    second_score = float(results[1].get("score") or 0.0) if len(results) > 1 else 0.0
    gap = max(0.0, top_score - second_score)
    lane_count = len(top.get("candidate_recall_lanes") or [])
    lane_factor = min(1.0, lane_count / 4.0)
    confidence = min(
        1.0,
        (0.18 * min(1.0, max(0.0, top_score)))
        + (0.42 * coverage)
        + (0.18 * lane_factor)
        + (0.22 * min(1.0, gap)),
    )
    if confidence >= 0.68:
        quality = "best"
        missing = ""
    elif confidence >= 0.45:
        quality = "degraded"
        missing = "evidence is plausible but not sharply separated"
    else:
        quality = "uncertain"
        missing = "low term coverage or weak top-result separation"
    return {
        "quality": quality,
        "confidence": round(confidence, 3),
        "covered_terms": covered,
        "gap": round(gap, 4),
        "missing_signal": missing,
        "suggested_followup_probe": (
            f"skygrep --agent-context --include "
            f"{str(Path(str(top.get('path', ''))).parent) or '<scope>'!r} {query!r}"
            if quality != "best"
            else ""
        ),
        "recall_paths": int(telemetry.get("total_paths") or 0),
    }


def attach_agent_evidence_summary(
    query: str,
    results: list[dict],
    telemetry: dict[str, Any],
) -> list[dict]:
    """Annotate ranked results with an agent-readable evidence bundle.

    The public JSON remains a list of result objects. New fields are optional,
    so older consumers keep working while desktop/agent callers can read
    confidence, ranking reasons, and targeted follow-up probes.
    """

    if not results:
        return results
    summary = _confidence_for_results(query, results, telemetry)
    likely_files = [str(result.get("path") or "") for result in results if result.get("path")]
    for index, result in enumerate(results):
        path = str(result.get("path") or "")
        lanes = list(result.get("candidate_recall_lanes") or [])
        text = f"{path}\n{result.get('snippet') or result.get('chunk') or ''}"
        evidence_terms = _covered_query_terms(query, text, max_terms=12)
        stype = source_type(path)
        result["source_type"] = stype
        result["search_intent"] = telemetry.get("intent") or classify_agent_query_intent(query)
        result["evidence_terms"] = evidence_terms
        result["confidence"] = summary["confidence"] if index == 0 else None
        result["why_ranked"] = {
            "lanes": lanes,
            "source_type": stype,
            "source_prior": round(_source_type_prior(str(result["search_intent"]), path), 3),
            "covered_terms": evidence_terms,
            "score": round(float(result.get("score") or 0.0), 4),
        }
        result["evidence_bundle"] = {
            "primary_anchor": {
                "path": path,
                "start_line": result.get("start_line"),
                "end_line": result.get("end_line"),
            },
            "supporting_chunks": result.get("supporting_chunks", []),
            "why_ranked": result["why_ranked"],
        }
    results[0]["agent_summary"] = {
        "quality": summary["quality"],
        "confidence": summary["confidence"],
        "likely_files": likely_files[:8],
        "primary_anchor": likely_files[0] if likely_files else "",
        "missing_signal": summary["missing_signal"],
        "suggested_followup_probe": summary["suggested_followup_probe"],
        "covered_terms": summary.get("covered_terms", []),
        "recall_paths": summary.get("recall_paths", 0),
    }
    return results


def merge_agent_results(
    query: str,
    result_groups: list[list[dict]],
    telemetry: dict[str, Any],
    *,
    top_k: int,
) -> list[dict]:
    """Path-level merge for agent evidence.

    Agent context wants candidate files, not many chunks from the same file.
    Keep the best-scoring representation per path, then reattach a fresh
    confidence summary after all lanes have voted.
    """

    by_path: dict[str, dict] = {}
    for group in result_groups:
        for result in group:
            path = str(result.get("path") or "")
            if not path:
                continue
            if path not in by_path or float(result.get("score") or 0.0) > float(
                by_path[path].get("score") or 0.0
            ):
                by_path[path] = result
    ranked = sorted(by_path.values(), key=lambda item: float(item.get("score") or 0.0), reverse=True)[:top_k]
    return attach_agent_evidence_summary(query, ranked, telemetry)


def _db_paths(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute("SELECT DISTINCT file FROM chunks").fetchall()
    except sqlite3.OperationalError:
        return []
    return [str(row[0]) for row in rows if row and row[0]]


def _add(
    scores: dict[str, float],
    lanes: dict[str, set[str]],
    path: str,
    lane: str,
    score: float,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
) -> None:
    if not path or not _matches_filters(path, include_patterns, exclude_patterns):
        return
    scores[path] = scores.get(path, 0.0) + score
    lanes[path].add(lane)


def _path_token_recall(
    conn: sqlite3.Connection,
    terms: list[str],
    *,
    root: Path,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    scores: dict[str, float],
    lanes: dict[str, set[str]],
) -> int:
    if not terms:
        return 0
    norm_terms = [_norm_token(term) for term in terms if _norm_token(term)]
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    hits = 0
    for path in _db_paths(conn):
        try:
            search_path = str(Path(path).resolve().relative_to(root_resolved))
        except (OSError, ValueError):
            search_path = Path(path).name
        path_lc = search_path.lower()
        path_norm = _norm_token(search_path)
        matched = {
            term
            for term, norm in zip(terms, norm_terms)
            if term in path_lc or (norm and norm in path_norm)
        }
        if not matched:
            continue
        _add(
            scores,
            lanes,
            path,
            "path",
            0.75 + (0.15 * len(matched)),
            include_patterns,
            exclude_patterns,
        )
        hits += 1
    return hits


def _sqlite_chunk_recall(
    conn: sqlite3.Connection,
    terms: list[str],
    *,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    scores: dict[str, float],
    lanes: dict[str, set[str]],
    per_term_limit: int,
) -> int:
    hits = 0
    for term in terms:
        seen_for_term: set[str] = set()
        for variant in _surface_variants(term):
            try:
                rows = conn.execute(
                    """
                    SELECT DISTINCT file
                    FROM chunks
                    WHERE LOWER(chunk) LIKE ?
                    LIMIT ?
                    """,
                    (f"%{variant.lower()}%", per_term_limit),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                path = str(row[0])
                if path in seen_for_term:
                    continue
                seen_for_term.add(path)
                _add(
                    scores,
                    lanes,
                    path,
                    "chunk",
                    0.55,
                    include_patterns,
                    exclude_patterns,
                )
                hits += 1
    return hits


def _symbol_recall(
    conn: sqlite3.Connection,
    terms: list[str],
    *,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    scores: dict[str, float],
    lanes: dict[str, set[str]],
    per_term_limit: int,
) -> int:
    hits = 0
    for term in terms:
        seen_for_term: set[str] = set()
        for variant in _surface_variants(term):
            try:
                rows = conn.execute(
                    """
                    SELECT DISTINCT file
                    FROM symbols
                    WHERE (' ' || name_lower || ' ') LIKE ?
                       OR LOWER(name) = ?
                       OR LOWER(name) LIKE ?
                    LIMIT ?
                    """,
                    (
                        f"% {variant.lower()} %",
                        variant.lower(),
                        f"%{variant.lower()}%",
                        per_term_limit,
                    ),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                path = str(row[0])
                if path in seen_for_term:
                    continue
                seen_for_term.add(path)
                _add(
                    scores,
                    lanes,
                    path,
                    "symbol",
                    1.0,
                    include_patterns,
                    exclude_patterns,
                )
                hits += 1
    return hits


def _rg_path_recall(
    terms: list[str],
    root: Path,
    *,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    scores: dict[str, float],
    lanes: dict[str, set[str]],
    timeout: float,
    max_paths: int,
) -> int:
    rg = shutil.which("rg")
    if rg is None or not terms or not root.exists():
        return 0
    cmd = [rg, "-il", "-F", "--sort", "path"]
    for glob in (
        "!node_modules/**",
        "!.venv/**",
        "!venv/**",
        "!target/**",
        "!dist/**",
        "!build/**",
        "!Library/**",
    ):
        cmd.extend(["-g", glob])
    for pattern in include_patterns:
        cmd.extend(["-g", pattern])
    for pattern in exclude_patterns:
        cmd.extend(["-g", f"!{pattern}"])
    rg_terms: list[str] = []
    for term in terms:
        for variant in _surface_variants(term):
            if variant not in rg_terms:
                rg_terms.append(variant)
    for term in rg_terms[: max(len(terms), 16)]:
        cmd.extend(["-e", term])
    cmd.append(str(root))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.debug("candidate rg recall skipped: %s", exc)
        return 0
    hits = 0
    for line in proc.stdout.splitlines():
        path = line.strip()
        if not path:
            continue
        candidates = [path]
        p_obj = Path(path)
        if p_obj.is_absolute():
            try:
                candidates.append(p_obj.resolve().relative_to(root.resolve()).as_posix())
            except (OSError, ValueError):
                pass
        for candidate in dict.fromkeys(candidates):
            _add(
                scores,
                lanes,
                candidate,
                "rg",
                0.9,
                include_patterns,
                exclude_patterns,
            )
        hits += 1
        if hits >= max_paths:
            break
    return hits


def _source_type_recall(
    conn: sqlite3.Connection,
    intent: str,
    terms: list[str],
    *,
    root: Path,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    scores: dict[str, float],
    lanes: dict[str, set[str]],
    max_paths: int,
) -> int:
    """Add source-type candidates for intents where artifact kind matters.

    This is intentionally a recall lane, not a filter. For test-location
    queries, every test file is eligible, but path/token overlap still decides
    how much it contributes. That keeps "which test covers X" from being
    drowned by release notes mentioning X while avoiding a hard dependency on
    exact test names.
    """

    if intent != "test_location":
        return 0
    hits = 0
    norm_terms = [_norm_token(term) for term in terms if _norm_token(term)]
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    for path in _db_paths(conn):
        if source_type(path) != "test":
            continue
        try:
            rel = str(Path(path).resolve().relative_to(root_resolved))
        except (OSError, ValueError):
            rel = path
        rel_norm = _norm_token(rel)
        overlap = sum(1 for term in norm_terms if term and term in rel_norm)
        _add(
            scores,
            lanes,
            path,
            "source-type",
            1.10 + (0.20 * overlap),
            include_patterns,
            exclude_patterns,
        )
        hits += 1
        if hits >= max_paths:
            break
    return hits


def _explicit_include_recall(
    conn: sqlite3.Connection,
    *,
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    scores: dict[str, float],
    lanes: dict[str, set[str]],
    max_paths: int,
) -> int:
    if not include_patterns:
        return 0
    hits = 0
    for path in _db_paths(conn):
        if not _matches_filters(path, include_patterns, exclude_patterns):
            continue
        _add(
            scores,
            lanes,
            path,
            "include",
            0.35,
            include_patterns,
            exclude_patterns,
        )
        hits += 1
        if hits >= max_paths:
            break
    return hits


def recall_candidate_paths(
    conn: sqlite3.Connection,
    query: str,
    root: Path,
    *,
    include_patterns: tuple[str, ...] = (),
    exclude_patterns: tuple[str, ...] = (),
    max_terms: int = 12,
    max_paths: int = 120,
    rg_timeout: float = 1.25,
    intent: str | None = None,
) -> tuple[set[str], dict[str, Any]]:
    """Return generic high-recall candidate paths plus telemetry.

    The returned set is not a final answer and should not be treated as a hard
    semantic boundary unless the caller explicitly asked for a scope. It is a
    recall substrate: cheap lanes vote paths into the pool, and the normal
    retrieval scorer chooses evidence-bearing chunks from those paths.
    """

    terms = extract_query_terms(query, max_terms=max_terms)
    query_intent = intent or classify_agent_query_intent(query)
    scores: dict[str, float] = {}
    lanes: dict[str, set[str]] = defaultdict(set)
    lane_counts: dict[str, int] = {}

    lane_counts["include"] = _explicit_include_recall(
        conn,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        scores=scores,
        lanes=lanes,
        max_paths=max_paths,
    )
    lane_counts["path"] = _path_token_recall(
        conn,
        terms,
        root=root,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        scores=scores,
        lanes=lanes,
    )
    lane_counts["symbol"] = _symbol_recall(
        conn,
        terms,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        scores=scores,
        lanes=lanes,
        per_term_limit=max_paths,
    )
    lane_counts["chunk"] = _sqlite_chunk_recall(
        conn,
        terms,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        scores=scores,
        lanes=lanes,
        per_term_limit=max_paths,
    )
    lane_counts["rg"] = _rg_path_recall(
        terms,
        root,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        scores=scores,
        lanes=lanes,
        timeout=rg_timeout,
        max_paths=max_paths,
    )
    lane_counts["source_type"] = _source_type_recall(
        conn,
        query_intent,
        terms,
        root=root,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        scores=scores,
        lanes=lanes,
        max_paths=max_paths,
    )

    for path in list(scores):
        prior = _source_type_prior(query_intent, path)
        if prior:
            scores[path] += prior
            lanes[path].add(f"prior:{source_type(path)}")

    ranked = sorted(scores, key=lambda path: (-scores[path], path))[:max_paths]
    telemetry = {
        "path": "candidate-recall",
        "intent": query_intent,
        "terms": terms,
        "lane_counts": lane_counts,
        "path_lanes": {path: sorted(lanes[path]) for path in ranked},
        "path_scores": {path: round(scores[path], 3) for path in ranked},
        "total_paths": len(ranked),
    }
    return set(ranked), telemetry


def candidate_chunk_results(
    conn: sqlite3.Connection,
    query: str,
    candidate_paths: set[str],
    *,
    top_k: int = 10,
    languages: tuple[str, ...] = (),
    include_patterns: tuple[str, ...] = (),
    exclude_patterns: tuple[str, ...] = (),
    path_scores: dict[str, float] | None = None,
    path_lanes: dict[str, list[str]] | None = None,
    support_per_path: int = 2,
    intent: str | None = None,
) -> list[dict]:
    """Return best lexical-evidence chunks from recalled candidate files.

    Path recall answers "which files should be considered?" This function
    answers the next generic question: "which chunks inside those files best
    support the query surface form?" It is deliberately embedding-free and
    bounded to the already-recalled files, so it improves evidence coverage
    without introducing another slow semantic path.
    """

    if not candidate_paths:
        return []
    where = []
    params: list[Any] = []
    placeholders = ",".join("?" * len(candidate_paths))
    where.append(f"file IN ({placeholders})")
    params.extend(sorted(candidate_paths))
    if languages:
        lang_placeholders = ",".join("?" * len(languages))
        where.append(f"language IN ({lang_placeholders})")
        params.extend(languages)
    where_clause = f"WHERE {' AND '.join(where)}"
    try:
        rows = conn.execute(
            f"""
            SELECT id, file, chunk, language, start_line, end_line,
                   start_byte, end_byte
            FROM chunks
            {where_clause}
            """,
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    scored: list[dict] = []
    path_scores = path_scores or {}
    path_lanes = path_lanes or {}
    terms = extract_query_terms(query, max_terms=16)
    query_intent = intent or classify_agent_query_intent(query)
    symbol_anchors = _symbol_anchor_map(conn, candidate_paths, terms)
    for row in rows:
        path = str(row[1])
        if not _matches_filters(path, include_patterns, exclude_patterns):
            continue
        chunk = str(row[2])
        chunk_lex = lexical_score(query, "", chunk)
        path_lex = lexical_score(query, path, "")
        names = _identifier_names(chunk)
        identifier_hits = _identifier_term_hits(terms, names)
        identity = _identity_score(terms, chunk)
        lex = chunk_lex + (0.50 * path_lex)
        if lex <= 0.0 and not include_patterns:
            continue
        prior = _source_type_prior(query_intent, path)
        symbol_anchor_score, symbol_anchor_names = _symbol_anchor_score(
            symbol_anchors,
            path,
            row[4],
            row[5],
        )
        evidence_terms = _covered_query_terms(query, f"{path}\n{chunk}", max_terms=12)
        score = (
            float(lex)
            + (0.05 * identity)
            + symbol_anchor_score
            + (0.18 * len(evidence_terms))
            + (0.12 * float(path_scores.get(path, 0.0)))
            + (0.35 * prior)
        )
        lanes_for_path = path_lanes.get(path, [])
        if symbol_anchor_names and "symbol-anchor" not in lanes_for_path:
            lanes_for_path = list(lanes_for_path) + ["symbol-anchor"]
        scored.append(
            {
                "id": row[0],
                "file": path,
                "path": path,
                "chunk": chunk,
                "snippet": chunk,
                "language": row[3],
                "start_line": row[4],
                "end_line": row[5],
                "start_byte": row[6],
                "end_byte": row[7],
                "score": score,
                "semantic_score": 0.0,
                "lexical_score": float(lex),
                "chunk_lexical_score": float(chunk_lex),
                "path_lexical_score": float(path_lex),
                "identity_score": float(identity),
                "identifier_names": names,
                "symbol_anchor_names": symbol_anchor_names,
                "symbol_anchor_score": float(symbol_anchor_score),
                "identifier_term_hits": identifier_hits,
                "identifier_term_order": _identifier_term_order(
                    terms, identifier_hits
                ),
                "fallback": "candidate-recall",
                "candidate_recall": True,
                "candidate_recall_lanes": lanes_for_path,
                "source_type": source_type(path),
                "search_intent": query_intent,
                "evidence_terms": evidence_terms,
                "why_ranked": {
                    "lanes": lanes_for_path,
                    "source_type": source_type(path),
                    "source_prior": round(prior, 3),
                    "symbol_anchors": symbol_anchor_names,
                    "covered_terms": evidence_terms,
                    "score": round(float(score), 4),
                },
            }
        )
    scored.sort(key=lambda item: item.get("score", 0.0), reverse=True)
    best: dict[str, dict] = {}
    for item in scored:
        path = item["path"]
        if path not in best:
            best[path] = item
        if len(best) >= top_k:
            break
    _attach_supporting_chunks(best, scored, terms, per_path=support_per_path)
    return attach_agent_evidence_summary(query, list(best.values()), {
        "intent": query_intent,
        "total_paths": len(candidate_paths),
    })


def build_agent_context_results(
    conn: sqlite3.Connection,
    query: str,
    root: Path,
    *,
    top_k: int = 8,
    languages: tuple[str, ...] = (),
    include_patterns: tuple[str, ...] = (),
    exclude_patterns: tuple[str, ...] = (),
    max_paths: int | None = None,
    rg_timeout: float = 0.75,
    support_per_path: int = 2,
) -> tuple[list[dict], dict[str, Any]]:
    """Build the default agent-context evidence pack.

    This is the shared CLI/benchmark path: bounded rg/path/symbol/chunk recall,
    file-first chunk extraction, source-type priors, and an optional confidence
    summary. It deliberately avoids LLM/reranker calls so agent loops get a
    predictable first answer.
    """

    intent = classify_agent_query_intent(query)
    budget = max_paths or max(top_k * (8 if intent == "test_location" else 5), top_k)
    cands, telemetry = recall_candidate_paths(
        conn,
        query,
        root,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        max_paths=budget,
        rg_timeout=rg_timeout,
        intent=intent,
    )
    if not cands:
        telemetry["intent"] = intent
        return [], telemetry
    results = candidate_chunk_results(
        conn,
        query,
        cands,
        top_k=max(top_k, top_k * 3),
        languages=languages,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        path_scores=telemetry.get("path_scores", {}),
        path_lanes=telemetry.get("path_lanes", {}),
        support_per_path=support_per_path,
        intent=intent,
    )
    return merge_agent_results(query, [results], telemetry, top_k=top_k), telemetry
