"""Fail-closed integrity tests for the Mixedbread parity harness."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmarks.parity_vs_mixedbread import (
    is_skylakegrep_entry,
    resolve_mixedbread_bin,
)


_LOCAL_WRAPPER = """#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from skylakegrep.src.cli import main
if __name__ == "__main__":
    raise SystemExit(main())
"""

_MIXEDBREAD_STUB = """#!/usr/bin/env node
// @mixedbread/mgrep stub used only in unit tests
console.log("mgrep");
"""


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class ResolveMixedbreadBinTests(unittest.TestCase):
    def test_explicit_local_skygrep_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            local = _write_exec(Path(temp_dir) / "skygrep", _LOCAL_WRAPPER)
            with self.assertRaises(SystemExit) as ctx:
                resolve_mixedbread_bin(str(local))
            self.assertNotEqual(ctx.exception.code, 0)
            message = str(ctx.exception)
            self.assertIn("silently compare", message)
            self.assertIn("skylakegrep", message.lower())

    def test_path_which_skygrep_is_not_used_as_fallback(self):
        """PATH ``skygrep`` must never become the Mixedbread baseline."""
        with tempfile.TemporaryDirectory() as temp_dir:
            local = _write_exec(Path(temp_dir) / "skygrep", _LOCAL_WRAPPER)

            def fake_which(name: str):
                if name == "skygrep":
                    return str(local)
                return None

            with patch("benchmarks.parity_vs_mixedbread.shutil.which", side_effect=fake_which):
                with self.assertRaises(SystemExit) as ctx:
                    resolve_mixedbread_bin(None)
            message = str(ctx.exception)
            self.assertIn("PATH fallback to `skygrep` is refused", message)
            self.assertNotEqual(ctx.exception.code, 0)

    def test_which_skygrep_pointing_at_local_package_detected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            local = _write_exec(Path(temp_dir) / "skygrep", _LOCAL_WRAPPER)

            def fake_which(name: str):
                if name == "skygrep":
                    return str(local)
                return None

            with patch("benchmarks.parity_vs_mixedbread.shutil.which", side_effect=fake_which):
                self.assertTrue(is_skylakegrep_entry(local))
                with self.assertRaises(SystemExit):
                    resolve_mixedbread_bin(str(local))

    def test_explicit_mixedbread_mgrep_accepted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mx = _write_exec(Path(temp_dir) / "mgrep", _MIXEDBREAD_STUB)
            resolved = resolve_mixedbread_bin(str(mx))
            self.assertEqual(Path(resolved), mx.resolve())

    def test_path_mgrep_accepted_when_not_local(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            mx = _write_exec(Path(temp_dir) / "mgrep", _MIXEDBREAD_STUB)

            def fake_which(name: str):
                if name == "mgrep":
                    return str(mx)
                return None

            with patch("benchmarks.parity_vs_mixedbread.shutil.which", side_effect=fake_which):
                resolved = resolve_mixedbread_bin(None)
            self.assertEqual(Path(resolved), mx.resolve())


if __name__ == "__main__":
    unittest.main()
