import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from skylakegrep.src import cli as cli_module
from skylakegrep.src import config as cfg_mod
from skylakegrep.src.storage import init_db, store_chunks_batch


def _legacy_relative_row(file: str) -> dict:
    """A chunk row as written by older releases: ``chunks.file`` stored
    relative to the project root (``skygrep index .`` from the project)."""

    return {
        "file": file,
        "chunk": "legacy chunk",
        "language": "markdown",
        "chunk_index": 0,
        "file_mtime": 1.0,
        "start_line": 1,
        "end_line": 1,
        "start_byte": 0,
        "end_byte": 12,
        "embedding": [1.0, 0.0],
    }


class FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]


WATCH_TEST_INTERVAL = 7777


def _interrupt_watch_sleep(seconds):
    """Stop ``watch`` after its first pass through the loop.

    Only the watch loop sleeps for ``WATCH_TEST_INTERVAL`` seconds; other
    ``time.sleep`` callers (e.g. ``subprocess`` waiting on ``git rev-parse``
    inside ``project_root``) must keep working normally.
    """

    if seconds == WATCH_TEST_INTERVAL:
        raise KeyboardInterrupt


class IndexDbScopeTests(unittest.TestCase):
    """``index <path>`` / ``watch <path>`` must resolve the target DB from
    ``<path>``, not from the current working directory."""

    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        base = Path(self._temp.name)
        self.cwd_project = base / "cwd_project"
        self.target_project = base / "target_project"
        self.repos_dir = base / "repos"
        self.cwd_project.mkdir()
        self.target_project.mkdir()
        self.repos_dir.mkdir()
        (self.target_project / "notes.md").write_text(
            "skygrep resolves the index database per project."
        )

        self._old_cwd = os.getcwd()
        os.chdir(self.cwd_project)

        self._old_env_db = os.environ.pop("SKYGREP_DB_PATH", None)

        self._repos_patch = patch.object(
            cfg_mod, "PROJECT_INDEX_DIR", self.repos_dir
        )
        self._repos_patch.start()

    def tearDown(self):
        self._repos_patch.stop()
        if self._old_env_db is not None:
            os.environ["SKYGREP_DB_PATH"] = self._old_env_db
        os.chdir(self._old_cwd)
        self._temp.cleanup()

    def _cwd_db(self) -> Path:
        return cfg_mod.project_db_path(self.cwd_project)

    def _target_db(self) -> Path:
        return cfg_mod.project_db_path(self.target_project)

    def test_index_path_writes_to_target_project_db(self):
        runner = CliRunner()
        with patch.object(cli_module, "get_embedder", return_value=FakeEmbedder()):
            result = runner.invoke(
                cli_module.cli, ["index", str(self.target_project)]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(
            self._target_db().exists(),
            f"expected target-project DB at {self._target_db()}; "
            f"repos dir contains {sorted(p.name for p in self.repos_dir.iterdir())}",
        )
        self.assertFalse(
            self._cwd_db().exists(),
            "index <path> leaked chunks into the CWD project's DB",
        )

    def test_index_path_reset_does_not_delete_cwd_project_db(self):
        sentinel = b"cwd project's precious index"
        self._cwd_db().write_bytes(sentinel)
        self._target_db().write_bytes(b"stale target index")

        runner = CliRunner()
        with patch.object(cli_module, "get_embedder", return_value=FakeEmbedder()):
            result = runner.invoke(
                cli_module.cli, ["index", str(self.target_project), "--reset"]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            self._cwd_db().read_bytes(),
            sentinel,
            "index <path> --reset deleted the CWD project's DB",
        )
        self.assertNotEqual(
            self._target_db().read_bytes(),
            b"stale target index",
            "--reset did not rebuild the target project's DB",
        )

    def test_index_env_override_still_wins(self):
        explicit = Path(self._temp.name) / "explicit.db"
        os.environ["SKYGREP_DB_PATH"] = str(explicit)
        try:
            runner = CliRunner()
            with patch.object(
                cli_module, "get_embedder", return_value=FakeEmbedder()
            ):
                result = runner.invoke(
                    cli_module.cli, ["index", str(self.target_project)]
                )
        finally:
            os.environ.pop("SKYGREP_DB_PATH", None)

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(explicit.exists())
        self.assertFalse(self._target_db().exists())

    def test_index_subdir_of_git_repo_indexes_whole_repo_into_root_db(self):
        subprocess.run(
            ["git", "init", "--quiet", str(self.target_project)],
            check=True,
            capture_output=True,
        )
        subdir = self.target_project / "docs"
        subdir.mkdir()
        (subdir / "guide.md").write_text("indexing a subdirectory")

        runner = CliRunner()
        with patch.object(cli_module, "get_embedder", return_value=FakeEmbedder()):
            result = runner.invoke(cli_module.cli, ["index", str(subdir)])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(
            self._target_db().exists(),
            "index <repo>/<subdir> must land in the repo root's DB so that "
            "searches run inside the repo can open it",
        )
        self.assertFalse(cfg_mod.project_db_path(subdir).exists())
        # The repo-root DB carries full-index markers afterwards, so it must
        # actually contain the whole repo — not just the subdirectory.
        indexed_files = {
            row[0]
            for row in init_db(self._target_db())
            .execute("SELECT DISTINCT file FROM chunks")
            .fetchall()
        }
        self.assertTrue(
            any(f.endswith("notes.md") for f in indexed_files),
            f"repo-root file missing from index; indexed: {sorted(indexed_files)}",
        )
        self.assertTrue(
            any(f.endswith("guide.md") for f in indexed_files),
            f"subdir file missing from index; indexed: {sorted(indexed_files)}",
        )

    def test_index_replaces_legacy_relative_rows_without_duplicates(self):
        # Older releases stored ``chunks.file`` relative to the project root.
        # ``index <abs path>`` run from a different CWD must still recognize
        # those rows as belonging to the target project — not resolve them
        # against the caller's CWD, leave them stale, and add duplicates.
        conn = init_db(self._target_db())
        store_chunks_batch(conn, [_legacy_relative_row("notes.md")])
        conn.close()

        runner = CliRunner()
        with patch.object(cli_module, "get_embedder", return_value=FakeEmbedder()):
            result = runner.invoke(
                cli_module.cli, ["index", str(self.target_project)]
            )

        self.assertEqual(result.exit_code, 0, result.output)
        rows = [
            row[0]
            for row in init_db(self._target_db())
            .execute("SELECT DISTINCT file FROM chunks")
            .fetchall()
        ]
        notes_rows = [f for f in rows if f.endswith("notes.md")]
        self.assertEqual(
            len(notes_rows),
            1,
            f"legacy relative row must be replaced, not duplicated; rows: {rows}",
        )
        self.assertTrue(Path(notes_rows[0]).is_absolute(), notes_rows)

    def test_watch_subdir_resolves_legacy_rows_against_project_root(self):
        subprocess.run(
            ["git", "init", "--quiet", str(self.target_project)],
            check=True,
            capture_output=True,
        )
        subdir = self.target_project / "docs"
        subdir.mkdir()
        (subdir / "guide.md").write_text("watching a subdirectory")
        # Legacy relative rows in the shared project DB: one inside the
        # watched scope (must be replaced), one outside it (must survive).
        conn = init_db(self._target_db())
        store_chunks_batch(
            conn,
            [
                _legacy_relative_row("docs/guide.md"),
                _legacy_relative_row("notes.md"),
            ],
        )
        conn.close()

        runner = CliRunner()
        with patch.object(cli_module, "get_embedder", return_value=FakeEmbedder()):
            with patch.object(cli_module.time, "sleep", _interrupt_watch_sleep):
                result = runner.invoke(
                    cli_module.cli,
                    ["watch", str(subdir), "--interval", str(WATCH_TEST_INTERVAL)],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        rows = [
            row[0]
            for row in init_db(self._target_db())
            .execute("SELECT DISTINCT file FROM chunks")
            .fetchall()
        ]
        guide_rows = [f for f in rows if f.endswith("guide.md")]
        self.assertEqual(
            len(guide_rows),
            1,
            f"in-scope legacy row must be replaced, not duplicated; rows: {rows}",
        )
        self.assertTrue(Path(guide_rows[0]).is_absolute(), guide_rows)
        self.assertIn(
            "notes.md",
            rows,
            "legacy row outside the watched scope must not be evicted",
        )

    def test_watch_subdir_of_git_repo_watches_only_subdir_into_root_db(self):
        subprocess.run(
            ["git", "init", "--quiet", str(self.target_project)],
            check=True,
            capture_output=True,
        )
        subdir = self.target_project / "docs"
        subdir.mkdir()
        (subdir / "guide.md").write_text("watching a subdirectory")

        # Invoke through a relative path from outside the repo: stored chunk
        # paths must not depend on the caller's CWD.
        relative_subdir = os.path.relpath(subdir, os.getcwd())
        runner = CliRunner()
        with patch.object(cli_module, "get_embedder", return_value=FakeEmbedder()):
            with patch.object(cli_module.time, "sleep", _interrupt_watch_sleep):
                result = runner.invoke(
                    cli_module.cli,
                    [
                        "watch",
                        relative_subdir,
                        "--interval",
                        str(WATCH_TEST_INTERVAL),
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        # Unlike ``index``, ``watch`` does not stamp full-index markers, so it
        # keeps the user's scope: the repo root's DB, but only the watched
        # subdirectory's files.
        self.assertTrue(
            self._target_db().exists(),
            "watch <repo>/<subdir> must use the repo root's DB so that "
            "searches run inside the repo can open it",
        )
        watched_files = {
            row[0]
            for row in init_db(self._target_db())
            .execute("SELECT DISTINCT file FROM chunks")
            .fetchall()
        }
        self.assertTrue(
            any(f.endswith("guide.md") for f in watched_files),
            f"watched subdir file missing from index; indexed: {sorted(watched_files)}",
        )
        self.assertFalse(
            any(f.endswith("notes.md") for f in watched_files),
            "watch <repo>/<subdir> indexed files outside the watched "
            f"subdirectory; indexed: {sorted(watched_files)}",
        )
        for stored in watched_files:
            self.assertTrue(
                Path(stored).is_absolute() and Path(stored).exists(),
                "stored chunk path must not depend on the caller's CWD; "
                f"got {stored!r}",
            )

    def test_watch_path_uses_target_project_db(self):
        runner = CliRunner()
        with patch.object(cli_module, "get_embedder", return_value=FakeEmbedder()):
            with patch.object(cli_module.time, "sleep", _interrupt_watch_sleep):
                result = runner.invoke(
                    cli_module.cli,
                    [
                        "watch",
                        str(self.target_project),
                        "--interval",
                        str(WATCH_TEST_INTERVAL),
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertTrue(
            self._target_db().exists(),
            "watch <path> indexed into a DB other than the target project's",
        )
        self.assertFalse(self._cwd_db().exists())


if __name__ == "__main__":
    unittest.main()
