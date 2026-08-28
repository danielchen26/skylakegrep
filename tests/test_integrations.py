# SPDX-License-Identifier: Apache-2.0
"""Tests for skylakegrep.src.integrations and the `skygrep setup` CLI."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from skylakegrep.src import cli as cli_module
from skylakegrep.src import integrations as integ


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
            self.assertIn("skylakegrep semantic search", content)

    def test_registered_snippet_teaches_agent_depth_flags(self):
        content = integ.SNIPPET_BODY
        self.assertIn("--agent-fast", content)
        self.assertIn("--agent-context", content)
        self.assertIn("--agent-daemon", content)
        self.assertIn("--strict", content)
        self.assertIn("strict_verification", content)
        self.assertIn("--content --detail standard", content)
        self.assertIn("--content --detail full", content)
        self.assertIn("--answer --content", content)
        self.assertIn("--json --content --detail standard", content)
        self.assertIn("--top 10", content)
        self.assertIn("--no-content", content)
        self.assertIn("--no-rerank", content)
        self.assertIn("--no-llm-router", content)
        self.assertIn("--no-cascade", content)
        self.assertIn("--daemon-url", content)
        self.assertIn("Option playbook", content)
        self.assertIn("Path/location only", content)
        self.assertIn("Exact regex/raw grep", content)
        self.assertIn('--include "src/**"', content)
        self.assertIn("--explain", content)
        self.assertIn("Closed-loop policy", content)
        self.assertIn("final task quality", content)
        self.assertIn("project brief", content)
        self.assertEqual(integ.SNIPPET_VERSION, "agent-guidance-v5")

    def test_registration_status_detects_current_stale_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            missing = self._make(tmp, name="Missing", config_name="MISSING.md")
            self.assertEqual(missing.registration_status(), "missing")

            stale = self._make(tmp, name="Stale", config_name="STALE.md")
            stale.config_path.write_text(
                integ.BEGIN_MARKER
                + "\n\nold setup guidance\n"
                + integ.END_MARKER
                + "\n"
            )
            self.assertEqual(stale.registration_status(), "stale")

            current = self._make(tmp, name="Current", config_name="CURRENT.md")
            current.register()
            self.assertEqual(current.registration_status(), "current")

    def test_register_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            i = self._make(tmp)
            i.register()
            self.assertFalse(i.register())  # second call: no change

    def test_register_refreshes_existing_managed_block(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            i = self._make(tmp)
            old_block = (
                integ.BEGIN_MARKER
                + "\n\nold setup guidance\n"
                + integ.END_MARKER
                + "\n"
            )
            i.config_path.parent.mkdir(parents=True, exist_ok=True)
            i.config_path.write_text("# My instructions\n\n" + old_block + "\nKeep this.\n")

            self.assertTrue(i.register())
            content = i.config_path.read_text()
            self.assertIn("# My instructions", content)
            self.assertIn("Keep this.", content)
            self.assertIn("--json --content --detail standard", content)
            self.assertNotIn("old setup guidance", content)

    def test_setup_check_reports_stale_snippet_without_modifying(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            integration = integ.Integration(
                name="TestAgent",
                description="A test integration",
                config_path=tmp / "AGENTS.md",
                detection_paths=(tmp,),
                detection_binaries=(),
            )
            integration.config_path.write_text(
                "# User rules\n\n"
                + integ.BEGIN_MARKER
                + "\n\nold setup guidance\n"
                + integ.END_MARKER
                + "\n"
            )
            with patch.object(integ, "all_integrations", return_value=[integration]):
                runner = CliRunner()
                result = runner.invoke(cli_module.cli, ["setup", "--check"])
            self.assertEqual(result.exit_code, 1, result.output)
            self.assertIn("stale", result.output)
            self.assertIn("old setup guidance", integration.config_path.read_text())

    def test_refresh_registered_snippets_only_touches_managed_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            stale = self._make(tmp, name="Stale", config_name="STALE.md")
            fresh = self._make(tmp, name="Fresh", config_name="FRESH.md")
            unregistered = self._make(tmp, name="Plain", config_name="PLAIN.md")

            stale.config_path.write_text(
                integ.BEGIN_MARKER
                + "\n\nold setup guidance\n"
                + integ.END_MARKER
                + "\n"
            )
            fresh.register()
            unregistered.config_path.write_text("user-authored rules only\n")

            changed = integ.refresh_registered_snippets([stale, fresh, unregistered])
            self.assertEqual([item.name for item in changed], ["Stale"])
            self.assertIn("--json --content --detail standard", stale.config_path.read_text())
            self.assertEqual(unregistered.config_path.read_text(), "user-authored rules only\n")

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
            with patch("skylakegrep.src.cli.integrations_mod", integ):
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

    def test_setup_refreshes_already_registered_snippet(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            integration = integ.Integration(
                name="TestAgent",
                description="A test integration",
                config_path=tmp / "AGENTS.md",
                detection_paths=(tmp,),
                detection_binaries=(),
            )
            integration.config_path.write_text(
                "# User rules\n\n"
                + integ.BEGIN_MARKER
                + "\n\nold setup guidance\n"
                + integ.END_MARKER
                + "\n"
            )
            tmp_marker = tmp / "setup_done"
            with patch.object(integ, "SETUP_DONE_MARKER", tmp_marker):
                with patch.object(integ, "all_integrations", return_value=[integration]):
                    runner = CliRunner()
                    result = runner.invoke(cli_module.cli, ["setup"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("updated snippet", result.output)
            content = integration.config_path.read_text()
            self.assertIn("--json --content --detail standard", content)
            self.assertNotIn("old setup guidance", content)


if __name__ == "__main__":
    unittest.main()
