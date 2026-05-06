"""Cold-start query → graph seed-node mapping for v2 retrieval.

The hard requirement (`docs/plans/2026-05-05-graph-prior-folder-inference.md`
§ 11) is that the very first query — by a user with **zero history** — still
produces a rich seed set. That requires every matcher to be **content-only**:
they look at the query text and the indexed corpus, never at past hits.

Four matchers, each contributing scored seeds:

  1. **Filename match** — query tokens substring-match against indexed file
     basenames. Cheap, exact, often the strongest single signal.
  2. **Symbol match** — query tokens substring-match against the camelCase-
     split ``name_lower`` column of the symbols table. Covers code identifiers
     even when the file isn't named after the symbol.
  3. **Semantic match** — query embedding cosine ≥ τ against the per-file
     mean-pooled embeddings (``files.embedding``). Catches cases where no
     surface token overlaps but the topic does.
  4. **Path-token match** — query tokens substring-match against folder names
     anywhere in the file path. Catches cases like "auth refresh" → any file
     under ``…/auth/…``.

Returns a normalised seed distribution suitable for ``ppr_walk()``.

Design notes:
  - Everything is graph-node-id keyed at the boundary, so the caller can
    treat the seeds opaquely.
  - History (S6 in the plan) is **not** used here; if the caller wants to
    boost recently-accessed nodes, it does so on the returned dict.
  - Latency target: < 50 ms total for warm SQLite, since this runs on
    every cold-start cascade.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional

import numpy as np

from .graph_walk import (
    Graph,
    NODE_CHUNK,
    NODE_FILE,
    NODE_FOLDER,
    NODE_SYMBOL,
)


# ── Tokeniser ──────────────────────────────────────────────────────────────
# Matches identifier-shaped runs (`auth`, `refresh_token`, `JWT`) and
# camelCase boundaries inside them (`refreshToken` → `refresh`, `Token`).
# Drops common English stopwords + skygrep ergonomics tokens that carry no
# information about the target.
_TOKEN_PATTERN  = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_STOPWORDS = frozenset(
    "a an the of in on at by for to from is are was were be been being "
    "this that these those it its as if then so but and or not nor "
    "show find search where what when how why which who whom whose "
    "do does did done doing have has had give get tell me my our".split()
)
_MIN_TOKEN_LEN = 2


def tokenise_query(query: str) -> list[str]:
    """Lower-cased identifier-style token list, with camelCase split + stops.

    >>> tokenise_query("Where does refreshToken live?")
    ['refresh', 'token', 'live']
    """
    raw = _TOKEN_PATTERN.findall(query)
    out: list[str] = []
    for tok in raw:
        # Split camelCase: "refreshToken" → ["refresh", "Token"]
        for piece in _CAMEL_BOUNDARY.split(tok):
            piece = piece.lower()
            if len(piece) >= _MIN_TOKEN_LEN and piece not in _STOPWORDS:
                out.append(piece)
    # De-dupe but preserve order
    seen: set[str] = set()
    return [t for t in out if not (t in seen or seen.add(t))]


# ── Matcher 1: filename ────────────────────────────────────────────────────
def match_filenames(
    conn: sqlite3.Connection,
    tokens: list[str],
    *,
    limit_per_token: int = 10,
) -> dict[str, float]:
    """Find indexed files whose basename contains any query token.

    Returns ``{file_path: hit_count}``. **No preset score weight** — score
    is the raw count of distinct query tokens that hit the basename.
    Caller decides how to combine across matchers (currently: raw sum).
    """
    if not tokens:
        return {}
    out: dict[str, float] = {}
    for tok in tokens:
        like = f"%{tok}%"
        cur = conn.execute(
            "SELECT DISTINCT file FROM chunks "
            "WHERE LOWER(file) LIKE ? "
            "LIMIT ?",
            (like, limit_per_token),
        )
        for (path,) in cur.fetchall():
            base = Path(path).name.lower()
            if tok in base:           # ensure token actually hits basename, not just dir
                out[path] = out.get(path, 0.0) + 1.0
    return out


# ── Matcher 2: symbol ──────────────────────────────────────────────────────
def match_symbols(
    conn: sqlite3.Connection,
    tokens: list[str],
    *,
    limit_per_token: int = 20,
) -> dict[str, float]:
    """Find files containing symbols whose ``name_lower`` matches a token.

    The ``symbols`` table's ``name_lower`` column is camelCase-split lower-
    case (`LanguageModelClient` → `language model client`) — substring on
    that is more permissive than a plain identifier match.

    Returns ``{file_path: hit_count}``. **No preset score weight** — score
    is the raw count of (token, symbol) hits per file. The caller may
    decide whether to aggregate symbol hits and filename hits with equal
    weight (current default) or to derive different ratios from corpus
    statistics — but no constant ratio is hardcoded here.
    """
    if not tokens:
        return {}
    # Probe the table once — if symbols/L1 isn't populated, skip silently
    try:
        cur = conn.execute("SELECT 1 FROM symbols LIMIT 1")
        if cur.fetchone() is None:
            return {}
    except sqlite3.OperationalError:
        return {}

    out: dict[str, float] = {}
    for tok in tokens:
        like = f"%{tok}%"
        cur = conn.execute(
            "SELECT file FROM symbols WHERE name_lower LIKE ? LIMIT ?",
            (like, limit_per_token),
        )
        for (path,) in cur.fetchall():
            out[path] = out.get(path, 0.0) + 1.0
    return out


# ── Matcher 3: semantic ────────────────────────────────────────────────────
def match_semantic(
    conn: sqlite3.Connection,
    query_embedding: np.ndarray,
    *,
    top_k: int = 20,
    threshold: float = 0.45,
    expected_dim: Optional[int] = None,
) -> dict[str, float]:
    """Cosine of query embedding against the per-file mean embedding.

    Pulls all rows from the ``files`` table (typically O(file_count) which
    is < 10K for practical projects), computes cosine, returns top-K above
    threshold.

    Returns ``{file_path: cosine_score}``.
    """
    qv = np.asarray(query_embedding, dtype=np.float32)
    if qv.size == 0:
        return {}
    qn = float(np.linalg.norm(qv)) or 1.0
    qv_norm = qv / qn

    cur = conn.execute("SELECT file, embedding FROM files WHERE embedding IS NOT NULL")
    scored: list[tuple[str, float]] = []
    for path, blob in cur.fetchall():
        try:
            v = np.frombuffer(blob, dtype=np.float32)
        except Exception:
            continue
        if expected_dim is not None and v.size != expected_dim:
            continue
        vn = float(np.linalg.norm(v)) or 1.0
        score = float(np.dot(qv_norm, v / vn))
        if score >= threshold:
            scored.append((path, score))

    scored.sort(key=lambda kv: -kv[1])
    return {p: s for p, s in scored[:top_k]}


# ── Matcher 4: path-token ──────────────────────────────────────────────────
def match_path_tokens(
    conn: sqlite3.Connection,
    tokens: list[str],
    *,
    limit_per_token: int = 50,
) -> dict[str, float]:
    """Files whose path (any directory component) contains a query token.

    Returns ``{file_path: hit_count}``. **No preset score weight** —
    score is the raw count of query tokens whose lowercased form appears
    as a directory component in the path.
    """
    if not tokens:
        return {}
    out: dict[str, float] = {}
    for tok in tokens:
        like = f"%/{tok}/%"        # require boundary slashes — token IS a dir component
        cur = conn.execute(
            "SELECT DISTINCT file FROM chunks WHERE LOWER(file) LIKE ? LIMIT ?",
            (like, limit_per_token),
        )
        for (path,) in cur.fetchall():
            out[path] = out.get(path, 0.0) + 1.0
    return out


# ── Folder-node seeds (containment-aware) ──────────────────────────────────
def folders_from_paths(paths: dict[str, float]) -> dict[str, float]:
    """Lift file-path scores up to their parent folder. The parent gets the
    sum of its scored children, capped at 1.0.

    Used for emitting folder-grain seeds in addition to file-grain ones, so
    that PPR can flow from a query into a directory and out to its other
    files via the containment edge.
    """
    out: dict[str, float] = {}
    for path, score in paths.items():
        parent = str(Path(path).parent)
        out[parent] = min(1.0, out.get(parent, 0.0) + score * 0.4)
    return out


# ── Public API: query → seeds ──────────────────────────────────────────────
def query_to_seeds(
    graph: Graph,
    conn: sqlite3.Connection,
    query: str,
    *,
    query_embedding: Optional[np.ndarray] = None,
    extra_tokens: tuple[str, ...] = (),
    semantic_threshold: float = 0.45,
    semantic_expected_dim: Optional[int] = None,
) -> dict[int, float]:
    """Cold-start query → ``{node_id: probability}`` seed distribution.

    All four matchers run unconditionally; missing data sources (e.g. no
    symbols table) are silently skipped. The caller passes ``query_embedding``
    only when bge-m3 is warm — when not provided, the semantic matcher is
    skipped (the other three still produce useful seeds for cold projects).

    Returns a normalised distribution. Empty dict iff every matcher missed
    (very rare — typically means the query has no usable tokens AND no
    embedder).
    """
    tokens = tokenise_query(query)
    tokens.extend(t.lower() for t in extra_tokens if t)

    file_scores: dict[str, float] = {}

    for path, score in match_filenames(conn, tokens).items():
        file_scores[path] = file_scores.get(path, 0.0) + score
    for path, score in match_symbols(conn, tokens).items():
        file_scores[path] = file_scores.get(path, 0.0) + score
    if query_embedding is not None:
        sem = match_semantic(
            conn, query_embedding,
            threshold=semantic_threshold,
            expected_dim=semantic_expected_dim,
        )
        for path, score in sem.items():
            file_scores[path] = file_scores.get(path, 0.0) + score
    for path, score in match_path_tokens(conn, tokens).items():
        file_scores[path] = file_scores.get(path, 0.0) + score

    # 0.3.1: NO folder-grain seeds. Folder seeds caused PPR to spread the
    # walk's residual equally across every sibling file via the
    # ``contains`` outgoing edge — dominating the ranking with structural
    # noise (every file under the project root surfaced equally regardless
    # of query relevance). Bench evidence: 0.3.0 hit rate 2/5 dropped
    # _every_ query's actual answer behind ``__init__.py`` / ``cli.py`` /
    # ``bootstrap.py`` because those siblings shared the folder seed.
    # Folder-grain context still flows into the walk through the
    # containment edges *from* file→folder→file paths (a file's parent
    # folder has incoming contains edges); we just don't *seed* folders.
    seeds: dict[int, float] = {}
    for path, score in file_scores.items():
        nid = graph.upsert_node(NODE_FILE, path)
        seeds[nid] = seeds.get(nid, 0.0) + score

    if not seeds:
        return {}

    # Normalise to a probability distribution
    total = sum(seeds.values()) or 1.0
    return {n: s / total for n, s in seeds.items()}


__all__ = [
    "tokenise_query",
    "match_filenames",
    "match_symbols",
    "match_semantic",
    "match_path_tokens",
    "folders_from_paths",
    "query_to_seeds",
]
