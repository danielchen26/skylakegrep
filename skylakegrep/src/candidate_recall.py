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
    include_patterns: tuple[str, ...],
    exclude_patterns: tuple[str, ...],
    scores: dict[str, float],
    lanes: dict[str, set[str]],
) -> int:
    if not terms:
        return 0
    norm_terms = [_norm_token(term) for term in terms if _norm_token(term)]
    hits = 0
    for path in _db_paths(conn):
        path_lc = path.lower()
        path_norm = _norm_token(path)
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
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT file
                FROM chunks
                WHERE LOWER(chunk) LIKE ?
                LIMIT ?
                """,
                (f"%{term.lower()}%", per_term_limit),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            path = str(row[0])
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
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT file
                FROM symbols
                WHERE (' ' || name_lower || ' ') LIKE ?
                   OR LOWER(name) = ?
                LIMIT ?
                """,
                (f"% {term.lower()} %", term.lower(), per_term_limit),
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            path = str(row[0])
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
    for term in terms:
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
        _add(
            scores,
            lanes,
            path,
            "rg",
            0.9,
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
) -> tuple[set[str], dict[str, Any]]:
    """Return generic high-recall candidate paths plus telemetry.

    The returned set is not a final answer and should not be treated as a hard
    semantic boundary unless the caller explicitly asked for a scope. It is a
    recall substrate: cheap lanes vote paths into the pool, and the normal
    retrieval scorer chooses evidence-bearing chunks from those paths.
    """

    terms = extract_query_terms(query, max_terms=max_terms)
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

    ranked = sorted(scores, key=lambda path: (-scores[path], path))[:max_paths]
    telemetry = {
        "path": "candidate-recall",
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
        lex = chunk_lex + (0.20 * path_lex)
        if lex <= 0.0 and not include_patterns:
            continue
        score = (
            float(lex)
            + (0.08 * identity)
            + (0.03 * float(path_scores.get(path, 0.0)))
        )
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
                "identifier_term_hits": identifier_hits,
                "identifier_term_order": _identifier_term_order(
                    terms, identifier_hits
                ),
                "fallback": "candidate-recall",
                "candidate_recall": True,
                "candidate_recall_lanes": path_lanes.get(path, []),
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
    return list(best.values())
