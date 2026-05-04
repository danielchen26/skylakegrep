"""Unit tests for the v0.13.0 filename-lookup shortcut.

Same conservative philosophy as the v0.12.0 lexical content
shortcut — a false-positive that hijacks a semantic content query
and routes it to ``find -iname`` is much worse than a missed
shortcut. Every condition is tested for both branches.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from local_mgrep.src import auto_index


def _has_find() -> bool:
    return shutil.which("find") is not None


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


# ---- happy path -----------------------------------------------------


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_fires_on_explicit_lookup_intent(tmp_path):
    root = _project(
        tmp_path,
        {
            "a/EB1B_Denial_Analysis.pdf": "x",
            "a/Tianchi Chen EB-1B filing.pdf": "x",
            "b/unrelated.txt": "x",
        },
    )
    out = auto_index.filename_shortcut(
        "where is eb1b file?", root, top_k=10
    )
    assert out is not None, "filename intent + matching files must fire"
    assert all(r["fallback"] == "filename-lookup" for r in out)
    paths = [r["path"] for r in out]
    assert any("EB1B" in p or "EB-1B" in p for p in paths)


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_fires_on_show_me(tmp_path):
    root = _project(
        tmp_path,
        {"src/package.json": "{}", "src/other.py": "x"},
    )
    out = auto_index.filename_shortcut(
        "show me package.json", root, top_k=10
    )
    assert out is not None
    assert any("package.json" in r["path"] for r in out)


# ---- condition 1: no lookup intent ----------------------------------


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_skips_without_intent_word(tmp_path):
    """Pure content question — no 'find' / 'where' / 'file' word —
    must fall through so cascade handles it."""
    root = _project(
        tmp_path,
        {"src/auth/login.py": "def login(): pass"},
    )
    out = auto_index.filename_shortcut(
        "how does authentication work", root, top_k=10
    )
    assert out is None, "content question must fall through"


# ---- condition 2: no name-like token --------------------------------


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_skips_when_only_stopwords(tmp_path):
    root = _project(tmp_path, {"src/x.py": "x"})
    # Every token is a stop word — nothing to look up
    out = auto_index.filename_shortcut(
        "where is the file", root, top_k=10
    )
    assert out is None


# ---- condition 3: too many matches ----------------------------------


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_skips_when_too_many_files_match(tmp_path):
    files = {f"src/{i:03d}_doc.md": "x" for i in range(50)}
    root = _project(tmp_path, files)
    # 50 matches is too many — likely not a precise lookup
    out = auto_index.filename_shortcut(
        "find doc file", root, top_k=10, max_files=30
    )
    assert out is None


# ---- condition 4: no basename literally contains the token ---------


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_skips_when_token_only_appears_in_dir_path(tmp_path):
    """If the token only matches a parent dir name, that's a weak
    signal — fall through to content search."""
    root = _project(
        tmp_path,
        {"deeply/EB1B_archive/something_else.txt": "x"},
    )
    out = auto_index.filename_shortcut(
        "find foo file", root, top_k=10
    )
    assert out is None


# ---- shape ----------------------------------------------------------


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_results_carry_size_and_mtime_metadata(tmp_path):
    root = _project(
        tmp_path,
        {"docs/README.md": "# hello\n"},
    )
    out = auto_index.filename_shortcut("find README file", root, top_k=5)
    assert out is not None
    r = out[0]
    assert "size:" in r["snippet"]
    assert "modified:" in r["snippet"]
    assert "type:" in r["snippet"]
    assert r["score"] == 1.0
    assert r["fallback"] == "filename-lookup"
    assert r["language"] == "md"


# ---- non-files (dirs) excluded --------------------------------------


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_directories_excluded(tmp_path):
    root = _project(
        tmp_path,
        {"src/eb1b_dir/inner.txt": "x"},
    )
    # The dir 'eb1b_dir' would match -iname; we want to ensure only
    # the file 'inner.txt' inside it (which doesn't match) is
    # considered, so the lookup falls through.
    out = auto_index.filename_shortcut(
        "find eb1b file", root, top_k=10
    )
    # No file basename contains 'eb1b' (only the directory name does),
    # so condition 4 fails and we fall through.
    assert out is None


# ---- empty input ----------------------------------------------------


def test_skips_on_empty_query(tmp_path):
    out = auto_index.filename_shortcut("   ", tmp_path, top_k=5)
    assert out is None
