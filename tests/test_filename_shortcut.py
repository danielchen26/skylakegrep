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

from skylakegrep.src import auto_index


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
            "a/CASE42_Project_Report.pdf": "x",
            "a/Example User CASE-42 project-file.pdf": "x",
            "b/unrelated.txt": "x",
        },
    )
    out = auto_index.filename_shortcut(
        "where is case42 file?", root, top_k=10
    )
    assert out is not None, "filename intent + matching files must fire"
    assert all(r["fallback"] == "filename-lookup" for r in out)
    paths = [r["path"] for r in out]
    assert any("CASE42" in p or "CASE-42" in p for p in paths)


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


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_llm_filename_intent_handles_chinese_lookup_with_embedded_identifier(tmp_path):
    """The LLM router can classify Chinese filename lookups correctly,
    but may return a descriptive primary_token like ``我的 CASE42 文件``.
    The filename tier should trust the intent and fall back to the
    identifier sub-token rather than dropping into semantic cascade."""

    class _Decision:
        intent = "filename"
        primary_token = "我的 CASE42 文件"

    root = _project(
        tmp_path,
        {
            "CASE42_Project_Report.pdf": "x",
            "Reference_letter_for_project_CASE42.pdf": "x",
            "unrelated.txt": "x",
        },
    )
    out = auto_index.filename_shortcut(
        "我的 CASE42 文件在哪", root, top_k=10, decision=_Decision()
    )
    assert out is not None
    paths = [Path(r["path"]).name for r in out]
    assert "CASE42_Project_Report.pdf" in paths
    assert "Reference_letter_for_project_CASE42.pdf" in paths
    assert {r["filename_token"] for r in out} == {"CASE42"}


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_llm_filename_intent_handles_cjk_filename_token(tmp_path):
    class _Decision:
        intent = "filename"
        primary_token = "合同"

    root = _project(
        tmp_path,
        {
            "我的合同扫描件.pdf": "x",
            "unrelated.txt": "x",
        },
    )
    out = auto_index.filename_shortcut(
        "我的合同文件在哪", root, top_k=10, decision=_Decision()
    )
    assert out is not None
    assert [Path(r["path"]).name for r in out] == ["我的合同扫描件.pdf"]
    assert {r["filename_token"] for r in out} == {"合同"}


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_filename_candidates_strip_cjk_wrappers_from_primary_token(tmp_path):
    class _Decision:
        intent = "filename"
        primary_token = "我的合同文件"

    root = _project(
        tmp_path,
        {
            "合同.pdf": "x",
            "unrelated.txt": "x",
        },
    )
    out = auto_index.filename_shortcut(
        "我的合同文件在哪", root, top_k=10, decision=_Decision()
    )
    assert out is not None
    assert [Path(r["path"]).name for r in out] == ["合同.pdf"]
    assert {r["filename_token"] for r in out} == {"合同"}


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
        {"deeply/CASE42_archive/something_else.txt": "x"},
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
        {"src/case42_dir/inner.txt": "x"},
    )
    # The dir 'case42_dir' would match -iname; we want to ensure only
    # the file 'inner.txt' inside it (which doesn't match) is
    # considered, so the lookup falls through.
    out = auto_index.filename_shortcut(
        "find case42 file", root, top_k=10
    )
    # No file basename contains 'case42' (only the directory name does),
    # so condition 4 fails and we fall through.
    assert out is None


# ---- empty input ----------------------------------------------------


def test_skips_on_empty_query(tmp_path):
    out = auto_index.filename_shortcut("   ", tmp_path, top_k=5)
    assert out is None


# ---- v0.14.1 token-priority fix -------------------------------------


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_identifier_token_with_digits_beats_longer_alpha_token(tmp_path):
    """The user's real complaint: query
        "where is case42 support letter evidence in all files?"
    has 'evidence' (8 chars, alpha) and 'case42' (4 chars, has digit).
    v0.14.0 picked 'evidence' by length and matched the wrong CSV.
    v0.14.1 must prefer the digit-bearing token."""
    root = _project(
        tmp_path,
        {
            "CASE42_project-file.pdf": "x",
            "CASE42_Project_Report.pdf": "x",
            "evidence_csv_red_herring.csv": "x",  # would catch on 'evidence'
        },
    )
    out = auto_index.filename_shortcut(
        "where is case42 support letter evidence in all files?",
        root,
        top_k=10,
    )
    assert out is not None, "must fire on filename intent"
    paths = [r["path"] for r in out]
    assert all("CASE42" in p or "case42" in p for p in paths), (
        f"must select 'case42' token (digit-bearing identifier), got {paths}"
    )
    assert not any("evidence_csv_red_herring" in p for p in paths)


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_separator_token_beats_plain_alpha(tmp_path):
    """`package.json` has a separator → preferred over `auth`."""
    root = _project(
        tmp_path,
        {"src/auth/login.py": "x", "package.json": "{}"},
    )
    out = auto_index.filename_shortcut(
        "find package.json auth file", root, top_k=10
    )
    assert out is not None
    paths = [r["path"] for r in out]
    assert any("package.json" in p for p in paths)


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_excludes_editor_and_app_lock_files(tmp_path):
    """v0.15.1 — Word `~$foo.docx` lock files, vim `.foo.swp`,
    emacs `.#foo` and `foo~` backups must not appear in results."""
    root = _project(
        tmp_path,
        {
            "Example User Case42 ProjectFile.docx": "real doc",
            "~$Example User Case42 ProjectFile.docx": "lock",
            ".#Example User Case42 ProjectFile.docx": "emacs lock",
            "Example User Case42 ProjectFile.docx~": "backup",
            ".Example User Case42 ProjectFile.docx.swp": "vim swap",
        },
    )
    out = auto_index.filename_shortcut(
        "find Case42 Application file", root, top_k=10
    )
    assert out is not None
    paths = [r["path"] for r in out]
    # Real doc must be returned
    assert any(
        Path(p).name == "Example User Case42 ProjectFile.docx" for p in paths
    )
    # No lock / swap / backup file
    for p in paths:
        name = Path(p).name
        assert not name.startswith("~$"), f"Word lock leaked: {name}"
        assert not name.startswith(".#"), f"emacs lock leaked: {name}"
        assert not name.endswith(".swp"), f"vim swap leaked: {name}"
        assert not name.endswith(".swo"), f"vim swap leaked: {name}"
        assert not name.endswith("~"), f"backup tilde leaked: {name}"


@pytest.mark.skipif(not _has_find(), reason="find not on PATH")
def test_falls_through_when_priority_token_fails(tmp_path):
    """If the priority token has no matches, we should try the next
    candidate, not give up immediately."""
    root = _project(
        tmp_path,
        {"docs/README.md": "x"},
    )
    # 'foo123' is digit-bearing and ranks first, but won't match
    # anything. The next priority candidate is 'README'.
    out = auto_index.filename_shortcut(
        "find foo123 README file", root, top_k=10
    )
    assert out is not None
    assert any("README" in r["path"] for r in out)
