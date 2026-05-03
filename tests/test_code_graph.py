"""Tests for the L4 file-export PageRank tiebreaker (code_graph.py).

Layout: a 4-file synthetic Rust project where ``lib`` is imported by 3
peers, ``util`` by 1, and ``orphan`` by none. Asserts:

  1. ``build_export_graph`` recovers the expected in-degrees.
  2. PageRank is monotonic in in-degree on this simple graph
     (lib > util > orphan).
  3. The tiebreaker re-orders two near-tied candidates **but does not**
     change the order when scores are clearly different — that's the
     regression guard against the abandoned P4-CGC global-prior approach.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_mgrep.src import code_graph
from local_mgrep.src.storage import (
    GRAPH_TIEBREAK_WEIGHT,
    TIEBREAK_EPS,
    _apply_graph_tiebreak,
    init_db,
)


def _make_rust_fixture(root: Path) -> dict[str, Path]:
    """Create the 4-file synthetic crate layout used by every test.

    Layout (per the L4 spec):
      - ``hub.rs``    — used by 3 peers (high in-degree)
      - ``util.rs``   — used by 1 peer
      - ``orphan.rs`` — used by 0 peers
      - ``main.rs``   — uses ``hub`` and ``util``

    Two extra consumer files reference ``hub`` only, lifting its in-degree
    so the PageRank hub > util > orphan ordering is unambiguous.

    Paths are returned resolved (``/private/var/...`` on macOS) so the
    keys match what ``build_export_graph`` produces.
    """

    src = root / "crates" / "foo" / "src"
    src.mkdir(parents=True, exist_ok=True)
    # lib.rs declares the four module siblings so the resolver can find them.
    lib = src / "lib.rs"
    lib.write_text(
        "pub mod hub;\npub mod util;\npub mod orphan;\npub mod main;\n"
        "pub mod consumer_a;\npub mod consumer_b;\n"
    )
    hub = src / "hub.rs"
    util = src / "util.rs"
    orphan = src / "orphan.rs"
    main = src / "main.rs"
    consumer_a = src / "consumer_a.rs"
    consumer_b = src / "consumer_b.rs"
    hub.write_text("pub fn hub_fn() {}\n")
    util.write_text("pub fn helper() {}\n")
    orphan.write_text("pub fn dangling() {}\n")
    main.write_text(
        "use crate::hub::hub_fn;\n"
        "use crate::util::helper;\n"
        "fn main() { hub_fn(); helper(); }\n"
    )
    consumer_a.write_text("use crate::hub::hub_fn;\nfn a() {}\n")
    consumer_b.write_text("use crate::hub::hub_fn;\nfn b() {}\n")
    return {
        "hub": hub.resolve(),
        "util": util.resolve(),
        "orphan": orphan.resolve(),
        "main": main.resolve(),
        "consumer_a": consumer_a.resolve(),
        "consumer_b": consumer_b.resolve(),
        "lib": lib.resolve(),
    }


class CodeGraphBuildTests(unittest.TestCase):
    def test_in_degrees_match_expected_layout(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = _make_rust_fixture(root)
            graph = code_graph.build_export_graph(root)

            hub = str(paths["hub"])
            util = str(paths["util"])
            orphan = str(paths["orphan"])
            self.assertIn(hub, graph)
            self.assertIn(util, graph)
            self.assertIn(orphan, graph)
            # ``hub`` is used by main, consumer_a, consumer_b plus the
            # ``mod hub;`` declaration in lib → in_degree ≥ 4.
            self.assertGreaterEqual(graph[hub]["in_degree"], 3)
            # ``util`` is used by main + the ``mod util;`` declaration → ≥ 1.
            self.assertGreaterEqual(graph[util]["in_degree"], 1)
            self.assertLess(graph[util]["in_degree"], graph[hub]["in_degree"])
            # ``orphan`` is referenced only via ``mod orphan;`` in lib (no
            # ``use`` site). Its in-degree is below util's because util
            # has both a ``mod`` and a ``use``.
            self.assertLess(graph[orphan]["in_degree"], graph[util]["in_degree"])

    def test_pagerank_monotonic_with_in_degree(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = _make_rust_fixture(root)
            graph = code_graph.build_export_graph(root)

            pr_hub = graph[str(paths["hub"])]["pagerank"]
            pr_util = graph[str(paths["util"])]["pagerank"]
            pr_orphan = graph[str(paths["orphan"])]["pagerank"]
            # Strictly: hub > util > orphan on this graph.
            self.assertGreater(pr_hub, pr_util)
            self.assertGreater(pr_util, pr_orphan)

    def test_populate_graph_table_writes_rows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _make_rust_fixture(root)
            db_path = root / "idx.db"
            conn = init_db(db_path)
            n = code_graph.populate_graph_table(conn, root)
            self.assertGreater(n, 0)
            row = conn.execute(
                "SELECT COUNT(*) FROM file_graph WHERE pagerank > 0"
            ).fetchone()
            self.assertGreater(row[0], 0)


class TiebreakRegressionTests(unittest.TestCase):
    """Guard the regression: clearly-different scores must not flip."""

    def _seed(self, root: Path) -> tuple[object, dict[str, Path]]:
        paths = _make_rust_fixture(root)
        db_path = root / "idx.db"
        conn = init_db(db_path)
        code_graph.populate_graph_table(conn, root)
        return conn, paths

    def test_tiebreaker_flips_near_ties(self):
        """A near-tie between hub and orphan tilts toward the hub."""

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            conn, paths = self._seed(root)
            # Construct a candidate list where the orphan is ahead by < EPS.
            # The hub has the larger pagerank; the tiebreaker should push it
            # past the orphan.
            candidates = [
                {"path": str(paths["orphan"]), "score": 0.500},
                {"path": str(paths["hub"]), "score": 0.499},
            ]
            fired = _apply_graph_tiebreak(conn, candidates)
            self.assertTrue(fired)
            self.assertEqual(candidates[0]["path"], str(paths["hub"]))

    def test_tiebreaker_does_not_flip_clear_gaps(self):
        """The P4-CGC regression: a clearly-better leaf must stay on top.

        The leaf (orphan, low pagerank) has a score 0.05 above the hub
        (very high pagerank). 0.05 >> TIEBREAK_EPS (0.005), so the
        tiebreaker MUST NOT fire. This is the exact failure mode that
        the abandoned P4-CGC global prior produced.
        """

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            conn, paths = self._seed(root)
            candidates = [
                {"path": str(paths["orphan"]), "score": 0.55},
                {"path": str(paths["hub"]), "score": 0.50},
            ]
            before = [c["path"] for c in candidates]
            fired = _apply_graph_tiebreak(conn, candidates)
            self.assertFalse(fired)
            self.assertEqual([c["path"] for c in candidates], before)

    def test_safety_property(self):
        """``GRAPH_TIEBREAK_WEIGHT`` is always ≤ ``TIEBREAK_EPS``."""

        self.assertLessEqual(GRAPH_TIEBREAK_WEIGHT, TIEBREAK_EPS)


if __name__ == "__main__":
    unittest.main()
