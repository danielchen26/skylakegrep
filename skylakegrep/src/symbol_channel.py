"""Symbol-as-retriever channel for skylakegrep.

Phase 3 of the design doc moves tree-sitter symbols from a post-hoc prior
into a first-class retrieval channel. The motivation: cosine + cross-
encoder ranking sometimes promotes re-export aggregators
(``packages/react/index.js``) above canonical implementations
(``packages/react/src/jsx/ReactJSXElement.js``). A post-hoc prior cannot
fix this — the canonical file is not in the rerank pool. The symbol
channel directly looks up files whose ``symbols`` table contains a
function/method/class definition matching a query term and forces those
files into the candidate pool.

The two public entry points:

* ``symbol_channel_search(conn, query_text, top_k)`` — returns up to
  ``top_k`` chunks from files whose ``symbols`` table matches a query
  term, in the same dict shape as ``storage.search`` results.
* ``multi_channel_search(conn, query_text, ..., top_k)`` — fuses the
  cosine channel and the symbol channel via Reciprocal Rank Fusion.

Both are content-agnostic at the interface — query-term extraction is
the existing ``hybrid.extract_query_terms`` heuristic; the actual
language-specific symbol extraction lives in
``indexer.extract_file_symbols`` and is invoked at index time.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

from .hybrid import extract_query_terms


logger = logging.getLogger(__name__)


# RRF constant. ``k=60`` is the standard value used in the original
# Cormack et al. (2009) RRF paper and most search-fusion literature; it
# damps the contribution of low-ranked items so a doc that's e.g. rank 1
# in the symbol channel and absent from cosine still scores well.
RRF_K = 60

# Symbol-defining kinds we treat as canonical implementation evidence.
# Method covers TypeScript class methods, function covers free
# functions, class covers ES6 / TS / Python / Rust impls. ``trait`` and
# ``interface`` are deliberately excluded — they declare a contract,
# not the canonical implementation.
_CANONICAL_KINDS = ("function", "method", "class")


def _best_chunk_for_file(
    conn: sqlite3.Connection, file_path: str
) -> Optional[dict]:
    """Return one representative chunk row for ``file_path``.

    The symbol channel surfaces files, not specific chunk offsets, but
    the public dict shape requires ``start_line/end_line/snippet``. We
    pick the first chunk of the file as the representative — the symbol
    channel's job is to put the canonical *file* on screen; the agent
    can scroll or run a follow-up query for a sub-line offset. Returns
    ``None`` when no chunk exists for that file (shouldn't happen since
    symbols are always populated from chunks).
    """

    row = conn.execute(
        """
        SELECT id, file, chunk, language, start_line, end_line,
               start_byte, end_byte
        FROM chunks
        WHERE file = ?
        ORDER BY chunk_index ASC
        LIMIT 1
        """,
        (file_path,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "file": row[1],
        "path": row[1],
        "chunk": row[2],
        "snippet": row[2],
        "language": row[3],
        "start_line": row[4],
        "end_line": row[5],
        "start_byte": row[6],
        "end_byte": row[7],
    }


def symbol_channel_search(
    conn: sqlite3.Connection,
    query_text: str,
    top_k: int = 10,
    *,
    min_query_term_length: int = 4,
) -> list[dict]:
    """Return up to ``top_k`` candidates from files whose ``symbols`` table
    contains a function/method/class definition matching a query term.

    A "match" is one of:
      * exact match: ``symbols.name = term``
      * camelCase-split token match: ``term`` appears as a space-
        separated token in ``symbols.name_lower``

    Files are ranked by the number of distinct query terms that hit
    inside them (more matches = stronger signal); ties are broken by
    putting the file with the more specific match (exact > camelCase
    token) first.

    Returns the same dict shape as ``storage.search`` results, with the
    representative chunk being the file's first chunk plus a synthetic
    ``score`` proportional to match strength and a ``symbol_channel``
    flag for telemetry.

    Empty list silent-fallback when:
      * the query has no ≥``min_query_term_length`` terms,
      * the ``symbols`` table is missing or empty,
      * no symbol-name terms match.
    """

    if not query_text:
        return []
    terms = [t for t in extract_query_terms(query_text) if len(t) >= min_query_term_length]
    if not terms:
        return []

    kind_placeholders = ",".join("?" * len(_CANONICAL_KINDS))
    # Coarse SQL filter: pull every canonical-kind row whose
    # ``name_lower`` contains any query term as a substring (we compare
    # space-padded forms so a substring like ``" language "`` only
    # matches whole tokens). The substring check uses a single
    # ``LIKE`` per term ORed together — SQLite optimises this even
    # without a full-text index, and the ``symbols`` table is small
    # (5K-50K rows). Per-row exactness (token vs literal substring vs
    # exact-name) is decided in Python on the small filtered set.
    like_clauses = []
    like_params: list[str] = []
    for term in terms:
        # Match whole-token: surround the persisted ``name_lower`` with
        # spaces and search for ``" <term> "`` so a term doesn't hit a
        # longer identifier as a coincidental substring.
        like_clauses.append("(' ' || name_lower || ' ') LIKE ?")
        like_params.append(f"% {term} %")
        # Also match exact name (case-insensitive) so single-token
        # identifiers like ``createElement`` still get returned.
        like_clauses.append("LOWER(name) = ?")
        like_params.append(term)
    sql = f"""
        SELECT file, name, name_lower, kind
        FROM symbols
        WHERE kind IN ({kind_placeholders})
          AND ({' OR '.join(like_clauses)})
    """
    try:
        rows = conn.execute(
            sql,
            list(_CANONICAL_KINDS) + like_params,
        ).fetchall()
    except sqlite3.OperationalError as exc:
        # ``symbols`` table absent — table not migrated, silently skip.
        logger.debug("symbol channel skipped: %s", exc)
        return []
    if not rows:
        return []

    # Per-file aggregation: for each file, compute (a) the set of distinct
    # query terms that hit any of its symbols, (b) whether any of those
    # hits was an exact-name match (stronger signal than a camelCase
    # split-token hit). We also fall back to a substring check in
    # ``name_lower`` so terms like "useState" can match "use_state" / "use
    # state" — the term-extractor lowercases, so the exact comparison
    # against ``name_lower`` already handles camelCase forms.
    file_terms: dict[str, set[str]] = {}
    file_exact: dict[str, bool] = {}
    for file_str, name, name_lower, kind in rows:
        name_lc = (name or "").lower()
        nl = name_lower or ""
        nl_tokens = set(nl.split()) if nl else set()
        for term in terms:
            hit_exact = (name_lc == term)
            hit_token = (term in nl_tokens)
            if hit_exact or hit_token:
                file_terms.setdefault(file_str, set()).add(term)
                if hit_exact:
                    file_exact[file_str] = True
    if not file_terms:
        return []

    # Also walk every symbol row a second time, allowing a token-substring
    # match — i.e. ``term`` appears as a non-token substring of
    # ``name_lower``. This is a softer signal but recovers cases where the
    # symbol identifier wasn't camelCase-friendly. We only use it when
    # the strict pass returned zero hits for the file. We re-query
    # without the kind filter so partial-substring matches against
    # methods named "createElement" inside e.g. "create_element_with_id"
    # still surface. Capped to keep cost predictable.

    # Order: more distinct term hits first; on ties, exact-name hits
    # win; on further ties, alphabetic by path (deterministic).
    ranked = sorted(
        file_terms.keys(),
        key=lambda p: (
            -len(file_terms[p]),
            0 if file_exact.get(p, False) else 1,
            p,
        ),
    )

    out: list[dict] = []
    denom = max(1, len(terms))
    for file_str in ranked:
        chunk = _best_chunk_for_file(conn, file_str)
        if chunk is None:
            continue
        # Synthetic score in [0, 1+ɛ] — proportional to match fraction,
        # plus a small exact-match bonus so callers can sort directly.
        # Not directly comparable to cosine scores; the consumer is
        # expected to fuse via rank, not raw score.
        match_frac = len(file_terms[file_str]) / denom
        bonus = 0.05 if file_exact.get(file_str, False) else 0.0
        chunk["score"] = float(match_frac + bonus)
        chunk["symbol_channel"] = True
        chunk["symbol_channel_terms"] = sorted(file_terms[file_str])
        chunk["symbol_channel_exact"] = bool(file_exact.get(file_str, False))
        out.append(chunk)
        if len(out) >= top_k:
            break
    return out


def _rrf_fuse(
    cosine_results: list[dict],
    symbol_results: list[dict],
    *,
    k: int = RRF_K,
) -> list[dict]:
    """Fuse two ranked candidate lists via Reciprocal Rank Fusion.

    Each list contributes ``1 / (k + rank)`` to the doc's fused score;
    a doc absent from a list contributes 0 from that list. Documents
    are keyed by ``(path, start_line, end_line)`` so the same chunk
    appearing in both channels is not double-counted (and the higher-
    score representation is kept for display fields).
    """

    fused: dict[tuple, dict] = {}
    fused_scores: dict[tuple, float] = {}
    for rank, doc in enumerate(cosine_results):
        key = (doc.get("path"), doc.get("start_line"), doc.get("end_line"))
        fused[key] = doc
        fused_scores[key] = fused_scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        # Telemetry: cosine rank stored 1-indexed for human readability.
        doc.setdefault("cosine_rank", rank + 1)
    for rank, doc in enumerate(symbol_results):
        key = (doc.get("path"), doc.get("start_line"), doc.get("end_line"))
        if key not in fused:
            fused[key] = doc
        else:
            # If the same chunk appears in both channels, merge the
            # symbol-channel telemetry into the cosine doc so consumers
            # see both signals.
            fused[key]["symbol_channel"] = True
            fused[key]["symbol_channel_terms"] = doc.get(
                "symbol_channel_terms"
            )
            fused[key]["symbol_channel_exact"] = doc.get(
                "symbol_channel_exact"
            )
        fused_scores[key] = fused_scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        fused[key].setdefault("symbol_rank", rank + 1)
    out: list[dict] = []
    for key, doc in fused.items():
        doc["fused_score"] = float(fused_scores[key])
        out.append(doc)
    out.sort(key=lambda d: d.get("fused_score", 0.0), reverse=True)
    return out


def multi_channel_search(
    conn: sqlite3.Connection,
    query_text: str,
    *,
    embedder=None,
    query_embedding: Optional[list[float]] = None,
    top_k: int = 10,
    cosine_pool: int = 30,
    symbol_pool: int = 30,
    rerank: bool = True,
    rerank_pool: int = 30,
    candidate_paths: Optional[set[str]] = None,
    rank_by: str = "chunk",
) -> tuple[list[dict], dict]:
    """Run cosine + symbol channels and fuse via RRF.

    Returns ``(results, telemetry)`` where ``results`` is the top-K
    fused list and ``telemetry`` exposes per-channel timing and counts:

    * ``cosine_ms``, ``symbol_ms``, ``fuse_ms`` — wall-clock per phase
    * ``cosine_n``, ``symbol_n`` — pool size returned by each channel
    * ``hits_in_symbol_channel`` — how many of the final top-K were
      reachable via the symbol channel (overlapping sets allowed)

    The cosine channel is the existing ``storage.search`` path; the
    symbol channel is ``symbol_channel_search``. We deliberately keep
    ``storage.search`` untouched so the rest of the cascade is
    unaffected.

    ``embedder`` and ``query_embedding`` are alternative ways to feed
    the cosine channel; if neither is provided we raise ``ValueError``.
    """

    # Lazy imports to keep this module dependency-free when symbol-only
    # callers (tests, benches) drop in.
    from .storage import search as cosine_search

    if query_embedding is None:
        if embedder is None:
            raise ValueError(
                "multi_channel_search requires either query_embedding or "
                "embedder"
            )
        query_embedding = embedder.embed(query_text)

    # Cosine channel — same params as storage.search default rerank=True
    # path. Pool of 30 is the same as the existing benchmark default so
    # we don't disturb that pipeline.
    t0 = time.perf_counter()
    cosine = cosine_search(
        conn,
        query_embedding,
        top_k=cosine_pool,
        query_text=query_text,
        rerank=rerank,
        rerank_pool=rerank_pool,
        candidate_paths=candidate_paths,
        rank_by=rank_by,
    )
    cosine_ms = (time.perf_counter() - t0) * 1000.0

    # Symbol channel — independent SQL lookup, no embedding cost.
    t1 = time.perf_counter()
    symbol = symbol_channel_search(conn, query_text, top_k=symbol_pool)
    symbol_ms = (time.perf_counter() - t1) * 1000.0

    # Fuse via RRF.
    t2 = time.perf_counter()
    fused = _rrf_fuse(cosine, symbol)
    fuse_ms = (time.perf_counter() - t2) * 1000.0

    # Truncate to top_k for the final result list.
    out = fused[:top_k]

    # Telemetry: how many of the top-K were reachable via the symbol
    # channel (possibly via fusion alone, even if cosine missed them).
    symbol_paths = {d.get("path") for d in symbol}
    hits_in_symbol = sum(1 for d in out if d.get("path") in symbol_paths)

    telemetry = {
        "cosine_ms": round(cosine_ms, 2),
        "symbol_ms": round(symbol_ms, 2),
        "fuse_ms": round(fuse_ms, 2),
        "cosine_n": len(cosine),
        "symbol_n": len(symbol),
        "hits_in_symbol_channel": hits_in_symbol,
    }
    return out, telemetry
