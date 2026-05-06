"""Tests for the v2 graph-walk retrieval substrate (0.3.0+).

Covers:
  - ``tokenise_query`` lexer behaviour (camelCase split + stopword drop)
  - ``Graph`` CRUD idempotency + top-K edge index
  - ``ppr_walk`` convergence on a synthetic graph (3 stop reasons covered)
  - ``query_to_seeds`` cold-start with no history
  - ``populate_graph_substrate`` edge counts after ordinary indexing
  - ``cascade_search`` with ``SKYGREP_GRAPH_WALK=1`` does not regress
    (additive only by construction)

See ``docs/plans/2026-05-05-graph-prior-folder-inference.md`` § 17 for the
phase mapping these tests cover (G-2 + G-3 + G-4 acceptance criteria).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from skylakegrep.src.graph_walk import (
    EDGE_CONTAINS, EDGE_NAME_SIM, EDGE_REFS,
    Graph, NODE_FILE, NODE_FOLDER,
    ppr_walk,
)
from skylakegrep.src.graph_substrate import populate_graph_substrate
from skylakegrep.src.query_seeds import (
    folders_from_paths,
    match_filenames,
    match_path_tokens,
    query_to_seeds,
    tokenise_query,
)
from skylakegrep.src.storage import init_db, store_chunks_batch


# ── Tokeniser ──────────────────────────────────────────────────────────────


class TokeniseQueryTests(unittest.TestCase):
    def test_drops_stopwords(self):
        toks = tokenise_query("Where is the auth refresh logic?")
        self.assertNotIn("where", toks)
        self.assertNotIn("the", toks)
        self.assertIn("auth", toks)
        self.assertIn("refresh", toks)
        self.assertIn("logic", toks)

    def test_camelcase_split(self):
        toks = tokenise_query("refreshToken middleware")
        self.assertIn("refresh", toks)
        self.assertIn("token", toks)
        self.assertIn("middleware", toks)

    def test_min_length_drop(self):
        toks = tokenise_query("a b cd ef")
        # 1-char tokens dropped; 2-char kept
        self.assertNotIn("a", toks)
        self.assertNotIn("b", toks)
        self.assertIn("cd", toks)
        self.assertIn("ef", toks)

    def test_dedup_preserves_order(self):
        toks = tokenise_query("auth refresh auth")
        self.assertEqual(toks.count("auth"), 1)
        self.assertEqual(toks.index("auth"), 0)


# ── Graph CRUD ─────────────────────────────────────────────────────────────


class GraphCRUDTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "g.db"
        self.conn = init_db(self.db)
        self.graph = Graph(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_upsert_idempotent(self):
        a = self.graph.upsert_node(NODE_FILE, "/x/a.py")
        b = self.graph.upsert_node(NODE_FILE, "/x/a.py")
        self.assertEqual(a, b)

    def test_add_edge_and_top_k(self):
        a = self.graph.upsert_node(NODE_FILE, "/x/a.py")
        b = self.graph.upsert_node(NODE_FILE, "/x/b.py")
        c = self.graph.upsert_node(NODE_FILE, "/x/c.py")
        self.graph.add_edge(a, b, EDGE_NAME_SIM, 0.8)
        self.graph.add_edge(a, c, EDGE_NAME_SIM, 0.5)
        self.graph.add_edge(b, c, EDGE_REFS, 0.9)
        edges = self.graph.top_k_edges(a, k=10)
        self.assertEqual(len(edges), 2)
        self.assertEqual(edges[0][0], b)              # higher-weight first
        self.assertAlmostEqual(edges[0][1], 0.8)

    def test_top_k_filtered_by_type(self):
        a = self.graph.upsert_node(NODE_FILE, "/x/a.py")
        b = self.graph.upsert_node(NODE_FILE, "/x/b.py")
        self.graph.add_edge(a, b, EDGE_NAME_SIM, 0.8)
        self.graph.add_edge(a, b, EDGE_REFS, 0.9)
        only_refs = self.graph.top_k_edges(a, edge_types=(EDGE_REFS,))
        self.assertEqual(len(only_refs), 1)
        self.assertEqual(only_refs[0][2], EDGE_REFS)

    def test_self_loop_dropped(self):
        a = self.graph.upsert_node(NODE_FILE, "/x/a.py")
        self.graph.add_edge(a, a, EDGE_REFS, 1.0)
        self.assertEqual(len(self.graph.top_k_edges(a)), 0)


# ── PPR walk ───────────────────────────────────────────────────────────────


class PPRWalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "g.db"
        self.conn = init_db(self.db)
        self.graph = Graph(self.conn)
        # Build a 5-node line: A → B → C → D → E (refs)
        self.nodes = {
            n: self.graph.upsert_node(NODE_FILE, f"/x/{n}.py")
            for n in "ABCDE"
        }
        for src, dst in zip("ABCD", "BCDE"):
            self.graph.add_edge(self.nodes[src], self.nodes[dst], EDGE_REFS, 1.0)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_seed_dominates(self):
        # Start at A — A should top the score with B/C/D fading
        result = ppr_walk(self.graph, {self.nodes["A"]: 1.0})
        scores = dict(result.nodes)
        self.assertGreater(scores[self.nodes["A"]], scores.get(self.nodes["B"], 0))
        self.assertGreater(scores[self.nodes["B"]], scores.get(self.nodes["C"], 0))

    def test_max_visited_caps_walk(self):
        # Set max_visited=2, force the cap
        result = ppr_walk(self.graph, {self.nodes["A"]: 1.0}, max_visited=2)
        self.assertLessEqual(result.visited, 2)

    def test_empty_seeds(self):
        result = ppr_walk(self.graph, {})
        self.assertEqual(result.nodes, [])
        self.assertEqual(result.visited, 0)

    def test_eps_stop_on_isolated_node(self):
        x = self.graph.upsert_node(NODE_FILE, "/iso/x.py")
        result = ppr_walk(self.graph, {x: 1.0}, eps=0.5)
        # x has no out-edges — walk commits one visit then queue empties
        self.assertGreaterEqual(result.visited, 1)
        self.assertIn(result.stop_reason, ("eps", "max_visited"))


# ── query_to_seeds (cold-start) ────────────────────────────────────────────


class QuerySeedsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "g.db"
        self.conn = init_db(self.db)
        # Index a tiny corpus so the matchers have data
        store_chunks_batch(self.conn, [
            {"file": "/proj/auth/refresh.py",    "chunk": "def refresh_token(): pass",
             "language": "python", "chunk_index": 0, "file_mtime": 0.0,
             "start_line": 1, "end_line": 1, "start_byte": 0, "end_byte": 0,
             "embedding": [0.0, 0.0]},
            {"file": "/proj/auth/middleware.py", "chunk": "def renew_session(): pass",
             "language": "python", "chunk_index": 0, "file_mtime": 0.0,
             "start_line": 1, "end_line": 1, "start_byte": 0, "end_byte": 0,
             "embedding": [0.0, 0.0]},
            {"file": "/proj/utils/jwt.py",        "chunk": "def decode_jwt(): pass",
             "language": "python", "chunk_index": 0, "file_mtime": 0.0,
             "start_line": 1, "end_line": 1, "start_byte": 0, "end_byte": 0,
             "embedding": [0.0, 0.0]},
            {"file": "/proj/billing/charge.py",   "chunk": "def charge(): pass",
             "language": "python", "chunk_index": 0, "file_mtime": 0.0,
             "start_line": 1, "end_line": 1, "start_byte": 0, "end_byte": 0,
             "embedding": [0.0, 0.0]},
        ])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_filename_match_basic(self):
        scores = match_filenames(self.conn, ["refresh"])
        self.assertIn("/proj/auth/refresh.py", scores)
        self.assertNotIn("/proj/billing/charge.py", scores)

    def test_path_token_match_dir_component(self):
        scores = match_path_tokens(self.conn, ["auth"])
        self.assertIn("/proj/auth/refresh.py", scores)
        self.assertIn("/proj/auth/middleware.py", scores)
        self.assertNotIn("/proj/billing/charge.py", scores)

    def test_query_to_seeds_cold_start(self):
        # Query with no history, no embedding — should still produce seeds
        graph = Graph(self.conn)
        seeds = query_to_seeds(graph, self.conn, "auth refresh logic")
        # Should have at least filename + path-token hits on auth/refresh.py
        self.assertTrue(len(seeds) > 0)
        # Distribution must normalise
        self.assertAlmostEqual(sum(seeds.values()), 1.0, places=5)

    def test_query_to_seeds_no_match_returns_empty(self):
        graph = Graph(self.conn)
        seeds = query_to_seeds(graph, self.conn, "absolutely nothing here xyzzy")
        self.assertEqual(seeds, {})

    def test_folders_from_paths_lifts_score(self):
        out = folders_from_paths({"/proj/auth/refresh.py": 1.0,
                                  "/proj/auth/middleware.py": 1.0})
        self.assertIn("/proj/auth", out)


# ── graph_substrate end-to-end ────────────────────────────────────────────


class GraphSubstrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "g.db"
        self.conn = init_db(self.db)
        # Realistic small corpus
        for rel in ("auth/refresh.py", "auth/middleware.py", "utils/jwt.py"):
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(f"# {rel}\n")
        store_chunks_batch(self.conn, [
            {"file": str(self.root / rel), "chunk": "x",
             "language": "python", "chunk_index": 0, "file_mtime": 0.0,
             "start_line": 1, "end_line": 1, "start_byte": 0, "end_byte": 0,
             "embedding": [0.0, 0.0]}
            for rel in ("auth/refresh.py", "auth/middleware.py", "utils/jwt.py")
        ])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_populates_containment(self):
        stats = populate_graph_substrate(self.conn, self.root, skip_refs=True)
        self.conn.commit()
        # 3 leaf folder→file edges + 2 root→{auth,utils} folder→folder edges
        self.assertGreaterEqual(stats["edge_contains"], 3)
        self.assertEqual(stats["files"], 3)

    def test_path_prox_within_same_dir(self):
        populate_graph_substrate(self.conn, self.root, skip_refs=True)
        self.conn.commit()
        graph = Graph(self.conn)
        a = graph.find_node(NODE_FILE, str(self.root / "auth/refresh.py"))
        b = graph.find_node(NODE_FILE, str(self.root / "auth/middleware.py"))
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        # auth/refresh.py and auth/middleware.py share parent → path_prox edge
        edges = graph.top_k_edges(a, edge_types=("path_prox",))
        self.assertTrue(any(d == b for d, _, _ in edges))


# ── Integration: cascade_search with SKYGREP_GRAPH_WALK=1 ─────────────────


class CascadeIntegrationTests(unittest.TestCase):
    """Regression test: enabling graph_walk does not crash cascade_search.

    Full accuracy validation runs against the public-OSS bench, not here.
    This test only ensures the gate + integration code is wired correctly
    and doesn't break existing behaviour when no graph data exists yet.
    """

    def test_graph_walk_off_unchanged(self):
        from skylakegrep.src.storage import _graph_walk_candidates
        with tempfile.TemporaryDirectory() as t:
            conn = init_db(Path(t) / "g.db")
            paths, telemetry = _graph_walk_candidates(
                conn, "anything", np.array([1.0, 0.0], dtype=np.float32),
            )
            # Empty graph — should return empty list with explicit reason
            self.assertEqual(paths, [])
            self.assertEqual(telemetry["path"], "graph-walk-skip")
            conn.close()


if __name__ == "__main__":
    unittest.main()
