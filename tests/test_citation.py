# SPDX-License-Identifier: Apache-2.0
"""Guards the citation surfaces against drift.

``skylakegrep/src/citation.py`` is what ``skygrep cite`` prints and is the
only copy that ships inside the wheel. ``CITATION.cff`` is what GitHub and
Zenodo read. Both state the same facts, so a release that updates one and
forgets the other is a bug — these tests fail on that.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from skylakegrep import __version__
from skylakegrep.src import citation

REPO_ROOT = Path(__file__).resolve().parents[1]
CFF_PATH = REPO_ROOT / "CITATION.cff"


def _cff_scalars() -> dict[str, str]:
    """Top-level ``key: value`` scalars from CITATION.cff.

    Deliberately dependency-free: PyYAML is not a runtime or test
    dependency of this project, and the keys under test are flat scalars
    in a file this repository owns. Commented-out lines (the DOI template)
    are ignored, which is the point — a commented DOI must not satisfy an
    assertion about a live one.
    """

    scalars: dict[str, str] = {}
    for line in CFF_PATH.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r'([a-z-]+): "?([^"]*)"?', line)
        if match:
            scalars[match.group(1)] = match.group(2).strip()
    return scalars


def test_cff_exists_and_declares_supported_schema():
    assert CFF_PATH.is_file(), "CITATION.cff is what renders GitHub's Cite button"
    assert _cff_scalars()["cff-version"] == "1.2.0"


@pytest.mark.parametrize(
    "cff_key,module_value",
    [
        ("title", citation.TITLE),
        ("date-released", citation.RELEASE_DATE),
        ("license", citation.LICENSE),
        ("repository-code", citation.REPOSITORY),
        ("url", citation.HOMEPAGE),
    ],
)
def test_cff_matches_citation_module(cff_key: str, module_value: str):
    assert _cff_scalars()[cff_key] == module_value


def test_cff_version_matches_package_version():
    """The release protocol bumps the package; CITATION.cff must follow."""

    assert _cff_scalars()["version"] == __version__


def test_license_is_the_one_in_metadata():
    """Catches a relicense that updates LICENSE but not the citation surfaces."""

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'license = "{citation.LICENSE}"' in pyproject
    assert (REPO_ROOT / "LICENSE").read_text(encoding="utf-8").lstrip().startswith(
        "Apache License"
    )


def test_bibtex_is_wellformed_and_has_no_empty_fields():
    entry = citation.bibtex()
    assert entry.startswith("@software{chen_skylakegrep_2026,")
    assert entry.rstrip().endswith("}")
    assert entry.count("{") == entry.count("}")
    fields = dict(re.findall(r"^\s+(\w+)\s+= \{(.*)\},$", entry, re.MULTILINE))
    assert fields["version"] == __version__
    assert fields["license"] == citation.LICENSE
    assert all(value for value in fields.values())


def test_doi_prefers_the_version_pin_over_the_concept_doi(monkeypatch):
    """Once Zenodo mints DOIs, the version-pinned one must win."""

    monkeypatch.setattr(citation, "CONCEPT_DOI", "10.5281/zenodo.1")
    monkeypatch.setattr(citation, "VERSION_DOI", "10.5281/zenodo.2")
    assert citation.doi() == "10.5281/zenodo.2"
    assert "doi     = {10.5281/zenodo.2}" in citation.bibtex()
    assert "https://doi.org/10.5281/zenodo.2" in citation.apa()

    monkeypatch.setattr(citation, "VERSION_DOI", None)
    assert citation.doi() == "10.5281/zenodo.1"


def test_render_rejects_unknown_format():
    with pytest.raises(ValueError):
        citation.render("endnote")


@pytest.mark.parametrize("fmt", citation.FORMATS)
def test_every_advertised_format_renders_nonempty(fmt: str):
    assert citation.render(fmt).strip()


def test_cite_command_pipes_clean_bibtex_to_stdout():
    """`skygrep cite >> refs.bib` must not capture the advisory note."""

    proc = subprocess.run(
        [sys.executable, "-m", "skylakegrep.src.cli", "cite"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("@software{")
    assert "note:" not in proc.stdout
    assert "no DOI archived yet" in proc.stderr
