"""Bounded Personalized PageRank (PPR) for the v2 knowledge-graph retrieval
substrate. See ``docs/plans/2026-05-05-graph-prior-folder-inference.md``
§ 10 for the full algorithm rationale.

The walk is **bounded forward-push** (Andersen-Chung-Lang 2006): we maintain a
priority queue of nodes by their current residual score, expand the highest-
residual node, commit ``alpha`` to its score, and push ``(1 - alpha)`` to its
neighbours weighted by edge weight. We stop when either:

  * residual drops below ``eps`` for every queued node (σ-stop — clear
    winner found, no point exploring further);
  * we've visited ``max_visited`` nodes (hard cap — bounds latency
    even on pathologically dense graphs);
  * or wall-clock exceeds ``budget_ms`` (cooperative deadline).

The walk traverses only the **local neighbourhood** (max ~200 nodes out of
millions), which is what makes the v2 architecture latency-neutral despite
inferring over a richer information surface.

Public API:

  * ``Graph(conn)``                — thin wrapper exposing edge / node lookup
  * ``graph.upsert_node(kind, key)``
  * ``graph.add_edge(src, dst, type_, weight)``
  * ``graph.top_k_edges(node_id, k=8, edge_types=None)``
  * ``ppr_walk(graph, seeds, ...)`` — returns ``[(node_id, score), …]``
"""

from __future__ import annotations

import heapq
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional


# ── Edge type constants ────────────────────────────────────────────────────
# Stored as TEXT in SQLite for human-readability + cheap migration. Keep the
# string values stable across releases — schema-version churn would force a
# full graph rebuild.
EDGE_CONTAINS  = "contains"        # folder → file, file → chunk, chunk → symbol
EDGE_REFS      = "refs"            # file → file (imports / markdown links)
EDGE_NAME_SIM  = "name_sim"        # file ↔ file via Jaccard of basename tokens
EDGE_PATH_PROX = "path_prox"       # file ↔ file via shared-ancestor depth
EDGE_META      = "meta_cohort"     # file ↔ file with same (ext, ±mtime)
EDGE_SEMANTIC  = "semantic"        # chunk ↔ chunk via bge-m3 cosine ≥ τ
EDGE_CO_ACCESS = "co_access"       # files opened within Δt
EDGE_QUERY_HIT = "query_hit"       # token → file (TF-IDF on past hits)

ALL_EDGE_TYPES = (
    EDGE_CONTAINS, EDGE_REFS, EDGE_NAME_SIM, EDGE_PATH_PROX,
    EDGE_META, EDGE_SEMANTIC, EDGE_CO_ACCESS, EDGE_QUERY_HIT,
)


# ── Node kind constants ────────────────────────────────────────────────────
NODE_FILE   = "file"
NODE_FOLDER = "folder"
NODE_CHUNK  = "chunk"
NODE_SYMBOL = "symbol"
NODE_TOKEN  = "token"


# ── PPR-walk parameters ────────────────────────────────────────────────────
# Defaults tuned for skylakegrep's filesystem graph density. ``alpha`` is the
# canonical PPR restart probability; lower α explores further from seeds,
# higher α stays local. ``max_visited`` caps node count (latency invariant);
# ``eps`` is the residual cutoff — small enough that high-confidence walks
# still terminate quickly, large enough to short-circuit ambiguous walks.
DEFAULT_ALPHA       = 0.15
DEFAULT_EPS         = 1e-3
DEFAULT_MAX_VISITED = 200
DEFAULT_BUDGET_MS   = 1500
DEFAULT_TOP_K_EDGES = 8


@dataclass
class WalkResult:
    """Outcome of a single PPR walk."""
    nodes:           list[tuple[int, float]]   # ranked (node_id, score)
    visited:         int
    elapsed_ms:      float
    stop_reason:     str                       # "eps" | "max_visited" | "budget"


class Graph:
    """Thin wrapper around a SQLite connection exposing graph CRUD + lookup.

    All methods are O(1) or O(log N + K). Heavy operations (graph build,
    semantic-edge materialisation) live elsewhere; this class is the runtime
    walker's view of the data.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # ── Node CRUD ────────────────────────────────────────────────────────
    def upsert_node(self, kind: str, key: str) -> int:
        """Get-or-create node ID. Idempotent."""
        cur = self.conn.execute(
            "SELECT id FROM graph_node WHERE kind=? AND key=?", (kind, key)
        )
        row = cur.fetchone()
        if row:
            return int(row[0])
        cur = self.conn.execute(
            "INSERT INTO graph_node(kind, key) VALUES(?, ?)", (kind, key)
        )
        return int(cur.lastrowid)

    def get_node(self, node_id: int) -> Optional[tuple[str, str]]:
        """Return ``(kind, key)`` for ``node_id`` or ``None`` if absent."""
        cur = self.conn.execute(
            "SELECT kind, key FROM graph_node WHERE id=?", (node_id,)
        )
        row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def find_node(self, kind: str, key: str) -> Optional[int]:
        """Return node_id for ``(kind, key)`` or ``None`` if absent."""
        cur = self.conn.execute(
            "SELECT id FROM graph_node WHERE kind=? AND key=?", (kind, key)
        )
        row = cur.fetchone()
        return int(row[0]) if row else None

    # ── Edge CRUD ────────────────────────────────────────────────────────
    def add_edge(
        self, src_id: int, dst_id: int, type_: str, weight: float
    ) -> None:
        """Insert-or-replace an edge. Self-loops silently dropped."""
        if src_id == dst_id:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO graph_edge(src_id, dst_id, type, weight) "
            "VALUES(?, ?, ?, ?)",
            (src_id, dst_id, type_, max(0.0, min(1.0, weight))),
        )

    def add_edges_batch(self, edges: list[tuple[int, int, str, float]]) -> int:
        """Bulk insert. Returns number of rows written."""
        if not edges:
            return 0
        # Drop self-loops + clamp weights once, server-side INSERT is the
        # batch hot path
        cleaned = [
            (s, d, t, max(0.0, min(1.0, w)))
            for s, d, t, w in edges
            if s != d
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO graph_edge(src_id, dst_id, type, weight) "
            "VALUES(?, ?, ?, ?)",
            cleaned,
        )
        return len(cleaned)

    def top_k_edges(
        self,
        node_id: int,
        k: int = DEFAULT_TOP_K_EDGES,
        edge_types: Optional[tuple[str, ...]] = None,
    ) -> list[tuple[int, float, str]]:
        """Return up to ``k`` highest-weight outgoing edges from ``node_id``.

        Uses the ``(src_id, type, weight DESC)`` compound index — O(log N + K)
        regardless of total edge count.
        """
        if edge_types is None:
            cur = self.conn.execute(
                "SELECT dst_id, weight, type FROM graph_edge "
                "WHERE src_id=? ORDER BY weight DESC LIMIT ?",
                (node_id, k),
            )
        else:
            placeholders = ",".join("?" * len(edge_types))
            cur = self.conn.execute(
                f"SELECT dst_id, weight, type FROM graph_edge "
                f"WHERE src_id=? AND type IN ({placeholders}) "
                f"ORDER BY weight DESC LIMIT ?",
                (node_id, *edge_types, k),
            )
        return [(int(d), float(w), t) for d, w, t in cur.fetchall()]

    # ── Stats ────────────────────────────────────────────────────────────
    def stats(self) -> dict[str, int]:
        """Return rough counts (node total, edge total per type)."""
        out: dict[str, int] = {}
        cur = self.conn.execute("SELECT COUNT(*) FROM graph_node")
        out["nodes"] = int(cur.fetchone()[0])
        cur = self.conn.execute(
            "SELECT type, COUNT(*) FROM graph_edge GROUP BY type"
        )
        for t, n in cur.fetchall():
            out[f"edge_{t}"] = int(n)
        return out


# ── PPR walk ───────────────────────────────────────────────────────────────


def ppr_walk(
    graph: Graph,
    seeds: dict[int, float],
    *,
    alpha: float = DEFAULT_ALPHA,
    eps: float = DEFAULT_EPS,
    max_visited: int = DEFAULT_MAX_VISITED,
    budget_ms: float = DEFAULT_BUDGET_MS,
    edge_types: Optional[tuple[str, ...]] = None,
    top_k_edges_per_node: int = DEFAULT_TOP_K_EDGES,
) -> WalkResult:
    """Bounded forward-push Personalized PageRank.

    ``seeds`` is the personalization vector — a sparse map ``{node_id:
    initial_residual}`` which doesn't need to sum to 1 (we normalise on
    consumption). The walker pushes residual through the highest-priority
    nodes first and accumulates ``alpha`` of each visit into the score.

    Returns the ranked node list and walk telemetry. An empty seed map
    returns an empty result with ``stop_reason="eps"``.
    """
    if not seeds:
        return WalkResult([], visited=0, elapsed_ms=0.0, stop_reason="eps")

    t0 = time.perf_counter()
    deadline = t0 + budget_ms / 1000.0

    # Forward-push state: accumulated score, residual queue
    score: dict[int, float] = defaultdict(float)
    residual: dict[int, float] = dict(seeds)

    # heapq is a min-heap; we push (-residual, node) for max-heap behaviour
    queue: list[tuple[float, int]] = [(-r, n) for n, r in seeds.items()]
    heapq.heapify(queue)

    visited = 0
    stop_reason = "eps"

    while queue and visited < max_visited:
        if time.perf_counter() > deadline:
            stop_reason = "budget"
            break

        neg_r, node = heapq.heappop(queue)
        r = -neg_r

        # Stale entry — residual was reduced after this enqueue; the
        # current value lives in `residual[node]`
        if r > residual.get(node, 0.0) + 1e-9:
            continue
        if r < eps:
            stop_reason = "eps"
            break

        # Commit alpha-share of residual to the accumulated score
        score[node] += alpha * r

        # Push (1 - alpha) of residual outward through top-K edges
        out_edges = graph.top_k_edges(
            node, k=top_k_edges_per_node, edge_types=edge_types
        )
        if out_edges:
            total_w = sum(w for _, w, _ in out_edges) or 1.0
            push_total = (1 - alpha) * r
            for dst_id, w, _ in out_edges:
                push = push_total * (w / total_w)
                new_r = residual.get(dst_id, 0.0) + push
                residual[dst_id] = new_r
                heapq.heappush(queue, (-new_r, dst_id))
        # else: leaf node — residual just dissipates

        residual[node] = 0.0
        visited += 1
    else:
        # `while … else` runs when the loop exits via the visited cap or
        # an empty queue. Empty queue = full convergence (all residuals < eps).
        if visited >= max_visited:
            stop_reason = "max_visited"

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    ranked = sorted(score.items(), key=lambda kv: -kv[1])
    return WalkResult(
        nodes=ranked, visited=visited, elapsed_ms=elapsed_ms,
        stop_reason=stop_reason,
    )


__all__ = [
    "Graph",
    "WalkResult",
    "ppr_walk",
    "EDGE_CONTAINS", "EDGE_REFS", "EDGE_NAME_SIM", "EDGE_PATH_PROX",
    "EDGE_META", "EDGE_SEMANTIC", "EDGE_CO_ACCESS", "EDGE_QUERY_HIT",
    "ALL_EDGE_TYPES",
    "NODE_FILE", "NODE_FOLDER", "NODE_CHUNK", "NODE_SYMBOL", "NODE_TOKEN",
]
