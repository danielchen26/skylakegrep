# local-mgrep 0.15.1 — release notes

A small but real bug fix: editor / app **session-lock and swap files**
were leaking into filename-lookup results and producing confusing
extraction errors. Two-layer fix: filter at the find-command level
*and* detect at the binary-extract layer (defense in depth).

## The bug

Running `mgrep "where is eb1b file?"` in a directory where Word is
currently editing a `.docx` returned both:

```
╭─ Tianchi Chen Eb1b Application.docx                  docx   1.000   ← real doc
│ ...

╭─ ~$pert Letter for Tianchi Chen Eb1b Application.docx  docx   1.000  ← Word lock file
│ docx parser failed: Package not found at '~$pert Letter ...'
│ size: 0.2 KB ...
```

The second result is **MS Word's session-lock file** — Word creates a
sibling `~$<truncated-name>.docx` of ~0.2 KB containing session
metadata while editing. It is **not OOXML**; python-docx fails with
the unhelpful "Package not found".

## The fix

### 1. `find` filter at the source (`auto_index.filename_shortcut`)

The `find` invocation that drives the filename tier now excludes
the standard editor / app lock and swap-file conventions:

```
-not -name "~$*"     # MS Word, Excel, PowerPoint session locks
-not -name "*.swp"   # vim swap
-not -name "*.swo"   # vim swap (older)
-not -name ".#*"     # emacs lock
-not -name "*~"      # backup tilde (emacs / general)
```

These never appear in filename-lookup results.

### 2. Defense-in-depth at the extractor (`binary_extract.extract_docx`)

Even if a `~$*.docx` reached the docx parser by some other path,
we now detect the prefix and emit a friendly hint instead of the
mysterious "Package not found":

```
╭─ ~$Foo.docx                                           docx
│ Word session lock file (not the actual document); the real
│ doc is likely 'Foo.docx' or similar
╰─ ...
```

(Of course in the v0.15.1 routing this path normally never fires
because the find filter already removed the lock file from
results — this is the safety net.)

## Files changed

  - `local_mgrep/src/auto_index.py`: filename_shortcut find command
    excludes 5 lock/swap/backup naming conventions.
  - `local_mgrep/src/binary_extract.py`: `extract_docx` recognises
    `~$*` prefix as a Word lock file and returns a friendly hint.
  - `tests/test_filename_shortcut.py`: new test verifies all 5
    lock-file conventions are filtered while the real doc is
    still returned.
  - `tests/test_binary_extract.py`: new test for the lock-file
    detection branch.
  - `pyproject.toml`: 0.15.0 → 0.15.1.
  - `docs/local-mgrep-0.15.1.md` (this file).
  - `docs/index.html`, `docs/assets/{og-image.svg,og-image.png,
    hero-dark.svg}` — version stamp.
  - `docs/README.md`, `README.md` — index entry / release bullet.

## Compatibility

  - **120 / 120 unit tests pass** (118 prior + 2 new lock-file
    coverage).
  - All 0.4.x – 0.15.0 flags / env / per-project DB layout
    unchanged.
  - JSON output: byte-for-byte 0.15.0.
  - The retrieval pipeline (LLM router, cascade, lexical / filename
    shortcuts) is unchanged.

## Visual smoke

Before (v0.15.0):
```
$ mgrep "where is eb1b file?" -m 10
... 5 EB1B PDFs ...
╭─ ~$pert Letter for Tianchi Chen Eb1b Application.docx  docx  1.000
│ docx parser failed: Package not found at '...'
```

After (v0.15.1):
```
$ mgrep "where is eb1b file?" -m 10
... 5 EB1B PDFs ...     ← lock file silently filtered out
[1.650s · router=... · 4 filename + 0 lexical + cascade · ...]
```

## Install

```
pip install --upgrade local-mgrep
```
