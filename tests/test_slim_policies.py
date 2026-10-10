"""Benchmark helpers for the skygrep-slim and rg-agent policies."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from benchmarks import slim_policies as sp
from benchmarks.closed_loop_agent_benchmark import StepResult


class SlimPolicyHelperTests(unittest.TestCase):
    def test_batches_of_three(self):
        self.assertEqual(sp.batches(list("abcdefg")), [["a", "b", "c"], ["d", "e", "f"], ["g"]])

    def test_is_source_matches_product_suffixes(self):
        self.assertTrue(sp.is_source("pkg/mod.py"))
        self.assertTrue(sp.is_source("src/lib.rs"))
        self.assertFalse(sp.is_source("docs/guide.md"))

    @unittest.skipUnless(shutil.which("rg"), "ripgrep not installed")
    def test_rg_rank_prefers_files_matching_rare_terms_and_caps_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "paginator.py").write_text("class Paginator:\n    def page(self): pass\n")
            for i in range(300):
                (root / f"noise_{i}.py").write_text("def page(): pass\n")
            step, ranked = sp.rg_rank_step(root, ["paginator", "page"], includes=[], name="t", step_cls=StepResult)
        self.assertEqual(ranked[0], "paginator.py")
        self.assertEqual(step.paths[0], "paginator.py")
        # 301 files match "page" but the agent-visible listing stops at the cap.
        page_block = step.payload.split("## rg -c -i page")[1]
        self.assertLessEqual(len(page_block.strip().splitlines()) - 1, sp.AGENT_GREP_LINE_CAP)

    def test_read_lines_step_caps_lines(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "big.py").write_text("\n".join(f"x{i} = {i}" for i in range(5000)))
            step = sp.read_lines_step(root, "big.py", name="t", step_cls=StepResult, max_lines=100)
        self.assertEqual(step.paths, ["big.py"])
        self.assertIn("x99 = 99", step.payload)
        self.assertNotIn("x100 = 100", step.payload)


if __name__ == "__main__":
    unittest.main()
