"""Tests for the 0.5.3 lazy_indexer dedup + import-diffusion helpers.

These cover the deterministic, non-LLM, non-Ollama parts of the lazy
module so the contract is enforced by CI even on a machine without
Ollama running.
"""

from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from skylakegrep.src import lazy_indexer as LZ


class CrawlTreeTests(unittest.TestCase):
    def test_crawl_tree_skips_hidden_tool_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hidden = root / ".tool" / "target.py"
            hidden.parent.mkdir()
            hidden.write_text("hidden = True\n", encoding="utf-8")
            visible = root / "src" / "target.py"
            visible.parent.mkdir()
            visible.write_text("hidden = False\n", encoding="utf-8")

            files, _ = LZ.crawl_tree(root)

        self.assertIn(str(visible.resolve()), files)
        self.assertNotIn(str(hidden.resolve()), files)

    def test_crawl_tree_prunes_vendor_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            vendor = root / "node_modules" / "pkg" / "session.ts"
            vendor.parent.mkdir(parents=True)
            vendor.write_text("export const session = 1\n", encoding="utf-8")
            visible = root / "src" / "session.ts"
            visible.parent.mkdir()
            visible.write_text("export const session = 2\n", encoding="utf-8")

            files, _ = LZ.crawl_tree(root)

        self.assertIn(str(visible.resolve()), files)
        self.assertNotIn(str(vendor.resolve()), files)

    def test_crawl_tree_prunes_language_dependency_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            cache_file = root / "go" / "pkg" / "mod" / "dep" / "session.go"
            cache_file.parent.mkdir(parents=True)
            cache_file.write_text("package dep\n", encoding="utf-8")
            source_file = root / "go" / "src" / "app" / "session.go"
            source_file.parent.mkdir(parents=True)
            source_file.write_text("package app\n", encoding="utf-8")

            files, _ = LZ.crawl_tree(root)

        self.assertIn(str(source_file.resolve()), files)
        self.assertNotIn(str(cache_file.resolve()), files)


class StemFamilyKeyTests(unittest.TestCase):
    def test_numeric_prefix_files_collapse_to_same_key(self) -> None:
        a = LZ._stem_family_key(
            "django/contrib/admin/migrations/0001_initial.py"
        )
        b = LZ._stem_family_key(
            "django/contrib/admin/migrations/0002_logentry_remove_auto_add.py"
        )
        c = LZ._stem_family_key(
            "django/contrib/admin/migrations/0003_logentry_add_action_flag_choices.py"
        )
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_different_dirs_do_not_collapse(self) -> None:
        a = LZ._stem_family_key("a/migrations/0001_initial.py")
        b = LZ._stem_family_key("b/migrations/0001_initial.py")
        self.assertNotEqual(a, b)

    def test_short_stems_get_unique_key(self) -> None:
        # ``foo.py`` is 3 chars, below the 6-char threshold, so it
        # gets a unique key (== its full path).
        a = LZ._stem_family_key("a/foo.py")
        self.assertEqual(a, "a/foo.py")


class DedupeSeedGroupsTests(unittest.TestCase):
    def test_django_migration_family_collapses(self) -> None:
        files = [
            "django/contrib/admin/migrations/0001_initial.py",
            "django/contrib/admin/migrations/0002_logentry_remove_auto_add.py",
            "django/contrib/admin/migrations/0003_logentry_add_action_flag_choices.py",
            "django/contrib/admin/migrations/0004_alter_user_username_max_length.py",
            "django/db/migrations/executor.py",
            "django/db/migrations/migration.py",
        ]
        out = LZ._dedupe_seed_groups(files)
        # 4 same-family migration files collapse to 1; 2 distinct
        # files in /db/migrations/ remain (executor.py, migration.py).
        self.assertEqual(len(out), 3)
        self.assertIn("django/db/migrations/executor.py", out)
        self.assertIn("django/db/migrations/migration.py", out)

    def test_below_group_min_keeps_all(self) -> None:
        # Two same-family files do NOT collapse — group_min defaults
        # to 3 (preserves /__init__.py + /foo.py co-located pairs in
        # small dirs).
        files = [
            "x/0001_a.py",
            "x/0002_b.py",
        ]
        self.assertEqual(LZ._dedupe_seed_groups(files), files)

    def test_dedup_is_deterministic(self) -> None:
        files = [
            "x/0001_a.py", "x/0002_b.py", "x/0003_c.py",
        ]
        # Run twice — output must be identical (alphabetically-first
        # representative).
        out1 = LZ._dedupe_seed_groups(files)
        out2 = LZ._dedupe_seed_groups(files)
        self.assertEqual(out1, out2)
        self.assertEqual(out1, ["x/0001_a.py"])


class TokenShortcutSeedsTests(unittest.TestCase):
    def test_dedup_default_on(self) -> None:
        files = [
            f"x/migrations/000{i}_alter_table.py" for i in range(1, 8)
        ] + ["x/migrations/executor.py"]
        # Every file matches token "migrations" — without dedup, 8
        # would land in the seed list. With dedup, the 7 numeric-
        # prefix family collapses to 1 + executor.py = 2.
        out = LZ.token_shortcut_seeds(
            "where is the migrations runner", files, max_seeds=15,
        )
        self.assertLessEqual(len(out), 2)
        self.assertIn("x/migrations/executor.py", out)

    def test_dedup_can_be_disabled(self) -> None:
        files = [
            f"x/migrations/000{i}_alter_table.py" for i in range(1, 8)
        ]
        # When dedup=False, all 7 must survive (back-compat path for
        # callers that need raw token-match behaviour).
        out = LZ.token_shortcut_seeds(
            "migrations", files, max_seeds=15, dedupe=False,
        )
        self.assertEqual(len(out), 7)


class ExtractImportsTests(unittest.TestCase):
    def test_python_imports(self) -> None:
        text = """
from django.db import models
import django.urls.resolvers
import os
from .relative import thing
"""
        out = LZ.extract_imports(text)
        self.assertIn("django.db", out)
        self.assertIn("django.urls.resolvers", out)
        self.assertIn("os", out)

    def test_javascript_imports(self) -> None:
        text = """
import x from 'react';
import { y } from "react-dom";
const z = require('lodash');
"""
        out = LZ.extract_imports(text)
        self.assertIn("react", out)
        self.assertIn("react-dom", out)
        self.assertIn("lodash", out)

    def test_rust_use(self) -> None:
        text = "use std::collections::HashMap;"
        out = LZ.extract_imports(text)
        self.assertIn("std::collections::HashMap", out)

    def test_c_include(self) -> None:
        text = '#include <stdio.h>\n#include "myheader.h"'
        out = LZ.extract_imports(text)
        self.assertIn("stdio.h", out)
        self.assertIn("myheader.h", out)

    def test_dedup(self) -> None:
        text = "import os\nimport os\nimport sys"
        out = LZ.extract_imports(text)
        self.assertEqual(out.count("os"), 1)
        self.assertEqual(out.count("sys"), 1)


class ResolveImportsToPathsTests(unittest.TestCase):
    def test_python_dotted_path_resolves(self) -> None:
        project = [
            "/repo/django/db/models/__init__.py",
            "/repo/django/urls/resolvers.py",
            "/repo/tests/foo.py",
        ]
        imports = ["django.db.models", "django.urls.resolvers"]
        out = LZ.resolve_imports_to_paths(imports, project)
        # Both resolve.
        self.assertIn("/repo/django/db/models/__init__.py", out)
        self.assertIn("/repo/django/urls/resolvers.py", out)
        self.assertNotIn("/repo/tests/foo.py", out)

    def test_too_short_imports_ignored(self) -> None:
        project = ["/repo/a.py", "/repo/b/c.py"]
        out = LZ.resolve_imports_to_paths(["a", "b"], project)
        # 1-char and 2-char imports are below the 3-char floor, so
        # they don't resolve to anything (avoids spurious matches).
        self.assertEqual(out, [])

    def test_max_paths_cap(self) -> None:
        project = [f"/repo/django/contrib/auth/{i}.py" for i in range(50)]
        out = LZ.resolve_imports_to_paths(
            ["django.contrib.auth"], project, max_paths=5,
        )
        self.assertEqual(len(out), 5)


if __name__ == "__main__":
    unittest.main()
