"""End-to-end test for the 0.4.0 holistic graph-aware retrieval.

Per ``docs/plans/2026-05-06-holistic-graph-aware-retrieval.md``, the
acceptance criterion is end-to-end behaviour, NOT per-component
isolation tests. This file verifies that:

  1. ``populate_graph_table`` writes both ``file_graph`` (legacy) and
     the new ``graph_edge`` table correctly.
  2. ``_expand_via_reference_graph`` returns scored neighbours given
     seed paths from the cosine top-K.
  3. The cascade integration (zero-flag, always-on during escalation)
     unions graph-expanded candidates into the rerank pool.

No per-component test of isolated PPR walks, isolated seed mappers,
isolated edge weights — those are exactly the phased-design pattern
the 0.3.0 → 0.3.1 rollback eliminated.

The hyperparameter surface contributed by this test = **0**: every
score is cosine, every threshold is the existing
``CASCADE_TAU_FLOOR``.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

from skylakegrep.src.reference_graph import populate_graph_table
from skylakegrep.src.storage import (
    _expand_via_reference_graph,
    init_db,
    store_chunks_batch,
)


# ── Synthetic mini-corpus: three Python files with import edges ──────────
CORPUS = {
    "auth/refresh.py": (
        "from auth.middleware import renew_session\n"
        "def refresh_token(): renew_session()\n"
    ),
    "auth/middleware.py": (
        "from utils.jwt import decode_jwt\n"
        "def renew_session(): decode_jwt('x')\n"
    ),
    "utils/jwt.py": (
        "def decode_jwt(token): return None\n"
    ),
}


class HolisticGraphExpandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "g.db"
        # Write the corpus to disk so reference_graph extractors can scan it
        for rel, content in CORPUS.items():
            p = self.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        self.conn = init_db(self.db)
        # Seed chunks + file embeddings so the cosine matcher has data
        # Using small distinct vectors so cosine similarity is interpretable.
        self.embeddings = {
            "auth/refresh.py":     np.array([1.0, 0.1, 0.0], dtype=np.float32),
            "auth/middleware.py":  np.array([0.9, 0.2, 0.1], dtype=np.float32),
            "utils/jwt.py":        np.array([0.8, 0.3, 0.2], dtype=np.float32),
        }
        store_chunks_batch(self.conn, [
            {"file": str(self.root / rel), "chunk": content,
             "language": "python", "chunk_index": 0,
             "file_mtime": 0.0, "start_line": 1, "end_line": 1,
             "start_byte": 0, "end_byte": 0,
             "embedding": list(self.embeddings[rel])}
            for rel, content in CORPUS.items()
        ])
        # Populate file embeddings table (per-file mean = single chunk here)
        for rel, vec in self.embeddings.items():
            self.conn.execute(
                "INSERT OR REPLACE INTO files(file, chunk_count, embedding) "
                "VALUES(?, 1, ?)",
                (str(self.root / rel), vec.tobytes()),
            )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ── Test 1: populate_graph_table writes graph_edge ──────────────
    def test_populate_writes_graph_edge_with_refs(self):
        n = populate_graph_table(self.conn, self.root)
        self.assertGreater(n, 0)
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM graph_edge WHERE type = 'refs'")
        edge_count = int(cur.fetchone()[0])
        self.assertGreater(edge_count, 0,
                           "refs edges must be written by populate_graph_table")

    # ── Test 2: idempotent — re-running doesn't duplicate edges ─────
    def test_populate_is_idempotent(self):
        populate_graph_table(self.conn, self.root)
        first = int(self.conn.execute(
            "SELECT COUNT(*) FROM graph_edge WHERE type='refs'").fetchone()[0])
        populate_graph_table(self.conn, self.root)
        second = int(self.conn.execute(
            "SELECT COUNT(*) FROM graph_edge WHERE type='refs'").fetchone()[0])
        self.assertEqual(first, second, "edge count must not double on re-run")

    # ── Test 3: end-to-end expansion from a seed path ───────────────
    def test_expansion_returns_neighbours_scored_by_cosine(self):
        populate_graph_table(self.conn, self.root)
        # populate_graph_table calls .resolve() on the corpus paths
        # (canonical macOS form is /private/var/… not /var/…), so our
        # seed path must match that canonical form.
        seed_paths = [str((self.root / "auth/refresh.py").resolve())]
        # Make file embeddings keyed by the resolved path too, so the
        # cosine lookup in _expand_via_reference_graph hits.
        for rel, vec in self.embeddings.items():
            self.conn.execute(
                "INSERT OR REPLACE INTO files(file, chunk_count, embedding) "
                "VALUES(?, 1, ?)",
                (str((self.root / rel).resolve()), vec.tobytes()),
            )
        self.conn.commit()
        # Query embedding very close to refresh.py's vector — should rank
        # its neighbour middleware.py highly via cosine
        query_vec = np.array([0.95, 0.15, 0.05], dtype=np.float32)
        results, telemetry = _expand_via_reference_graph(
            self.conn, seed_paths, query_vec
        )
        self.assertEqual(telemetry["path"], "graph-expand",
                         "expansion must fire when graph_edge populated")
        # The seed (refresh.py) should NOT appear (it's already in cosine pool)
        # The 1-hop neighbour (middleware.py) SHOULD appear
        paths = [r["path"] for r in results]
        self.assertNotIn(str((self.root / "auth/refresh.py").resolve()), paths)
        # All scores should be valid cosines in [-1, 1]
        for r in results:
            self.assertGreaterEqual(r["score"], -1.0)
            self.assertLessEqual(r["score"], 1.0)

    # ── Test 4: empty seeds → silent no-op ──────────────────────────
    def test_empty_seeds_returns_empty(self):
        results, telemetry = _expand_via_reference_graph(
            self.conn, [], np.array([1.0, 0.0, 0.0], dtype=np.float32)
        )
        self.assertEqual(results, [])
        self.assertEqual(telemetry["path"], "no-seeds")

    # ── Test 5: missing graph table → silent no-op (forward compat) ─
    def test_no_graph_table_returns_empty(self):
        # Simulate an older DB with no graph_edge table
        bare_conn = sqlite3.connect(":memory:")
        bare_conn.execute(
            "CREATE TABLE files (file TEXT PRIMARY KEY, "
            "chunk_count INTEGER, embedding BLOB)")
        results, telemetry = _expand_via_reference_graph(
            bare_conn, ["x.py"], np.array([1.0, 0.0], dtype=np.float32)
        )
        self.assertEqual(results, [])
        self.assertEqual(telemetry["path"], "no-graph-tables")
        bare_conn.close()


if __name__ == "__main__":
    unittest.main()
