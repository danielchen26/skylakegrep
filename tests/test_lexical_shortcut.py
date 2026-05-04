"""Unit tests for the v0.12.0 lexical shortcut.

The shortcut must:
  - fire on lexical-friendly queries (short, path-token overlapping,
    clustered) and return rg results directly;
  - fall through (return None) on every other query so the semantic
    cascade is never bypassed by mistake.

Accuracy is the gold standard: a false-positive shortcut is much
worse than a missed shortcut, so every condition is tested for
both branches.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from local_mgrep.src import auto_index


# ---- helpers --------------------------------------------------------


def _has_rg() -> bool:
    return shutil.which("rg") is not None


def _project(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a tiny project tree from a {relpath: body} dict and return
    the project root."""
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


# ---- happy path -----------------------------------------------------


@pytest.mark.skipif(not _has_rg(), reason="ripgrep not installed")
def test_shortcut_fires_on_clean_lexical_match(tmp_path):
    root = _project(
        tmp_path,
        {
            "src/auth/login.py": "def login(): pass",
            "src/auth/logout.py": "def logout(): pass",
            "src/auth/token.py": "def refresh_token(): pass",
            "src/billing/invoice.py": "def make_invoice(): pass",
        },
    )
    out = auto_index.lexical_shortcut("auth login", root, top_k=5)
    assert out is not None, "shortcut should fire on clean lexical match"
    assert all(r["fallback"] == "rg-shortcut" for r in out)
    assert any("login.py" in r["path"] for r in out)


# ---- condition 1: too many query terms ------------------------------


@pytest.mark.skipif(not _has_rg(), reason="ripgrep not installed")
def test_shortcut_skips_on_long_descriptive_query(tmp_path):
    root = _project(
        tmp_path,
        {"src/auth/login.py": "def login(): pass"},
    )
    # 8 non-stopword terms — too descriptive to be plausibly lexical
    long_q = "where does the assistant call language model backend caller infrastructure"
    out = auto_index.lexical_shortcut(long_q, root, top_k=5)
    assert out is None, "long query must fall through to cascade"


# ---- condition 2: too many candidate files --------------------------


@pytest.mark.skipif(not _has_rg(), reason="ripgrep not installed")
def test_shortcut_skips_when_too_many_files_match(tmp_path):
    files = {f"src/util/util_{i}.py": "auth login" for i in range(15)}
    root = _project(tmp_path, files)
    out = auto_index.lexical_shortcut("auth login", root, top_k=5)
    assert out is None, "wide-scattered match must fall through"


# ---- condition 3: no path-token overlap -----------------------------


@pytest.mark.skipif(not _has_rg(), reason="ripgrep not installed")
def test_shortcut_skips_when_path_does_not_encode_query_tokens(tmp_path):
    # Files contain the tokens but their paths do NOT — typical
    # vocabulary-mismatch case where cascade should win.
    root = _project(
        tmp_path,
        {
            "src/zeta/alpha.py": "auth login flow here",
            "src/zeta/beta.py":  "auth login pipeline",
        },
    )
    out = auto_index.lexical_shortcut("auth login", root, top_k=5)
    assert out is None, "no path-token overlap must fall through"


# ---- condition 4: dirs scattered ------------------------------------


@pytest.mark.skipif(not _has_rg(), reason="ripgrep not installed")
def test_shortcut_skips_when_matches_scattered_across_many_dirs(tmp_path):
    root = _project(
        tmp_path,
        {
            "src/auth/login.py":      "auth login",
            "src/billing/login.py":   "auth login",
            "src/render/login.py":    "auth login",
            "src/network/login.py":   "auth login",
        },
    )
    out = auto_index.lexical_shortcut("auth login", root, top_k=5)
    assert out is None, "matches across >2 parent dirs must fall through"


# ---- empty / no-match -----------------------------------------------


@pytest.mark.skipif(not _has_rg(), reason="ripgrep not installed")
def test_shortcut_returns_none_on_no_rg_match(tmp_path):
    root = _project(tmp_path, {"src/util.py": "def foo(): pass"})
    out = auto_index.lexical_shortcut("nonexistent_token", root, top_k=5)
    assert out is None


def test_shortcut_returns_none_on_empty_query(tmp_path):
    out = auto_index.lexical_shortcut("   ", tmp_path, top_k=5)
    assert out is None


# ---- annotation / shape ---------------------------------------------


@pytest.mark.skipif(not _has_rg(), reason="ripgrep not installed")
def test_shortcut_results_are_annotated_rg_shortcut(tmp_path):
    root = _project(
        tmp_path,
        {
            "src/auth/login.py": "def login(): pass",
            "src/auth/token.py": "def refresh_token(): pass",
        },
    )
    out = auto_index.lexical_shortcut("auth login", root, top_k=5)
    assert out is not None
    for r in out:
        assert r["fallback"] == "rg-shortcut"
        # Shape compatibility with rg_fallback_results
        for key in ("path", "chunk", "snippet", "score", "start_line"):
            assert key in r
