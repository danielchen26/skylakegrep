# skylakegrep 0.7.3 — Apache-2.0 open-source distribution

0.7.3 is the distribution release that makes skylakegrep usable under a
standard open-source license and removes a real production footgun in the
CLI. It does not change the retrieval algorithm.

## What changed

- **Apache-2.0 relicense (#14).** `LICENSE`, `NOTICE`, `CITATION.cff`,
  `TRADEMARK.md`, `CONTRIBUTING.md`, README, and `pyproject.toml` now declare
  Apache-2.0. Commercial use, modification, and redistribution are permitted
  under those terms; trademarks stay under `TRADEMARK.md`.
- **`index` / `watch` / `--reset` follow the target path (#15).** The per-project
  SQLite DB is derived from `project_root(<path>)`, not from the process CWD.
  `watch` keeps the given subdirectory scope while writing into the project DB.
  `SKYGREP_DB_PATH` remains highest precedence. Regression coverage lives in
  `tests/test_index_db_scope.py`.
- **Live docs surfaces** (landing page, hero, roadmap) no longer advertise the
  old noncommercial license (#16).
- **Test alignment (#18).** The watch parity batch test pins the DB via
  `SKYGREP_DB_PATH` after the `#15` resolver change.

## Compatibility

- Existing 0.7.x indexes are byte-compatible; no rebuild is required for search
  quality. If you previously indexed a path from another working directory and
  landed chunks in the wrong project DB, re-run `skygrep index <path>` (and
  `--reset` only when you intend to wipe that project DB).
- Earlier PyPI artifacts through `0.7.2` were published under the previous
  license text on PyPI. This release replaces that packaging surface.

## Verification

- `tests/test_index_db_scope.py`: 8/8
- `tests.test_parity_batch.ParityBatchTests.test_watch_batches_new_files_across_boundaries`: OK
- GitHub license SPDX: Apache-2.0
- PyPI metadata for this version must show Apache-2.0 (not the previous
  noncommercial license text)

## Privacy

No user prompts, private paths, or personal data are included in this release.
