"""Tests for local_mgrep.src.integrations and the `mgrep setup` CLI."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from local_mgrep.src import cli as cli_module
from local_mgrep.src import integrations as integ


class IntegrationModelTests(unittest.TestCase):
    def _make(self, tmp: Path, name: str = "Test", config_name: str = "TEST.md") -> integ.Integration:
        return integ.Integration(
            name=name,
            description="A test integration",
            config_path=tmp / config_name,
            detection_paths=(tmp,),
            detection_binaries=(),
        )

    def test_register_creates_file_with_markers(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            i = self._make(tmp)
            self.assertTrue(i.register())
            content = i.config_path.read_text()
            self.assertIn(integ.BEGIN_MARKER, content)
            self.assertIn(integ.END_MARKER, content)
            self.assertIn("local-mgrep semantic search", content)

    def test_register_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            i = self._make(tmp)
            i.register()
            self.assertFalse(i.register())  # second call: no change

    def test_register_appends_without_clobbering_existing(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            i = self._make(tmp)
            existing = "# My instructions\n\nDo X.\n"
            i.config_path.parent.mkdir(parents=True, exist_ok=True)
            i.config_path.write_text(existing)
            self.assertTrue(i.register())
            content = i.config_path.read_text()
            self.assertTrue(content.startswith(existing))
            self.assertIn(integ.BEGIN_MARKER, content)

    def test_unregister_removes_only_managed_block(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            i = self._make(tmp)
            existing = "# My instructions\n\nDo X.\n"
            i.config_path.parent.mkdir(parents=True, exist_ok=True)
            i.config_path.write_text(existing)
            i.register()
            self.assertTrue(i.unregister())
            content = i.config_path.read_text()
            self.assertNotIn(integ.BEGIN_MARKER, content)
            self.assertNotIn(integ.END_MARKER, content)
            self.assertIn("Do X.", content)

    def test_unregister_no_op_when_not_registered(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            i = self._make(tmp)
            self.assertFalse(i.unregister())  # nothing to remove


class SetupCliTests(unittest.TestCase):
    def test_setup_list_does_not_modify(self):
        runner = CliRunner()
        with patch.object(integ, "_HOME", Path("/nonexistent")):
            with patch("local_mgrep.src.cli.integrations_mod", integ):
                result = runner.invoke(cli_module.cli, ["setup", "--list"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("Detected LLM CLIs", result.output)

    def test_setup_skip_marks_done(self):
        with tempfile.TemporaryDirectory() as d:
            tmp_marker = Path(d) / "setup_done"
            with patch.object(integ, "SETUP_DONE_MARKER", tmp_marker):
                self.assertFalse(integ.is_setup_done())
                runner = CliRunner()
                result = runner.invoke(cli_module.cli, ["setup", "--skip"])
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertTrue(integ.is_setup_done())


if __name__ == "__main__":
    unittest.main()
