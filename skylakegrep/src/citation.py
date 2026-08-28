# SPDX-License-Identifier: Apache-2.0
"""Citation metadata and renderers for ``skygrep cite``.

This module is the runtime source of truth for how to cite skylakegrep.
``CITATION.cff`` at the repository root carries the same facts for GitHub
and Zenodo, but it is not shipped inside the wheel — so the fields live
here, and ``tests/test_citation.py`` fails if the two ever drift apart.

Set :data:`CONCEPT_DOI` and :data:`VERSION_DOI` once a release has been
archived on Zenodo; every renderer picks them up automatically and the
"no DOI yet" advisory disappears.
"""

from __future__ import annotations

from . import __version__

TITLE = "skylakegrep: fully-offline semantic search over local files"
AUTHORS: tuple[tuple[str, str], ...] = (("Chen", "Tianchi"),)
RELEASE_DATE = "2026-08-14"
REPOSITORY = "https://github.com/danielchen26/skylakegrep"
HOMEPAGE = "https://danielchen26.github.io/skylakegrep/"
LICENSE = "Apache-2.0"

#: Zenodo concept DOI — always resolves to the latest archived release.
CONCEPT_DOI: str | None = None
#: Zenodo DOI pinned to the exact version in :data:`skylakegrep.__version__`.
VERSION_DOI: str | None = None

FORMATS = ("bibtex", "apa", "cff", "json")


def _year() -> str:
    return RELEASE_DATE.split("-")[0]


def _bibtex_key() -> str:
    return f"{AUTHORS[0][0].lower()}_skylakegrep_{_year()}"


def _authors_bibtex() -> str:
    return " and ".join(f"{family}, {given}" for family, given in AUTHORS)


def _authors_apa() -> str:
    return ", ".join(f"{family}, {given[0]}." for family, given in AUTHORS)


def doi() -> str | None:
    """Preferred DOI: the version-pinned one, else the concept DOI."""

    return VERSION_DOI or CONCEPT_DOI


def bibtex() -> str:
    fields = [
        ("author", _authors_bibtex()),
        ("title", TITLE),
        ("version", __version__),
        ("year", _year()),
        ("license", LICENSE),
        ("url", REPOSITORY),
    ]
    resolved = doi()
    if resolved:
        fields.insert(4, ("doi", resolved))
    width = max(len(name) for name, _ in fields)
    body = "\n".join(
        f"  {name.ljust(width)} = {{{value}}}," for name, value in fields
    )
    return f"@software{{{_bibtex_key()},\n{body}\n}}"


def apa() -> str:
    resolved = doi()
    tail = f" https://doi.org/{resolved}" if resolved else f" {REPOSITORY}"
    return (
        f"{_authors_apa()} ({_year()}). {TITLE} (Version {__version__}) "
        f"[Computer software].{tail}"
    )


def cff() -> str:
    lines = [
        "cff-version: 1.2.0",
        'message: "If you use skylakegrep in academic or published work, '
        'please cite it as below."',
        f'title: "{TITLE}"',
        "type: software",
        "authors:",
    ]
    for family, given in AUTHORS:
        lines += [f'  - family-names: "{family}"', f'    given-names: "{given}"']
    lines += [
        f'version: "{__version__}"',
        f'date-released: "{RELEASE_DATE}"',
        f'repository-code: "{REPOSITORY}"',
        f'url: "{HOMEPAGE}"',
        f"license: {LICENSE}",
    ]
    resolved = doi()
    if resolved:
        lines.append(f'doi: "{resolved}"')
    return "\n".join(lines)


def as_dict() -> dict[str, object]:
    payload: dict[str, object] = {
        "title": TITLE,
        "authors": [
            {"family_names": family, "given_names": given} for family, given in AUTHORS
        ],
        "version": __version__,
        "date_released": RELEASE_DATE,
        "repository_code": REPOSITORY,
        "url": HOMEPAGE,
        "license": LICENSE,
        "type": "software",
    }
    if CONCEPT_DOI:
        payload["concept_doi"] = CONCEPT_DOI
    if VERSION_DOI:
        payload["version_doi"] = VERSION_DOI
    return payload


def render(fmt: str) -> str:
    """Render the citation in ``fmt``; raises ``ValueError`` if unknown."""

    if fmt == "bibtex":
        return bibtex()
    if fmt == "apa":
        return apa()
    if fmt == "cff":
        return cff()
    if fmt == "json":
        import json

        return json.dumps(as_dict(), indent=2, ensure_ascii=False)
    raise ValueError(f"unknown citation format: {fmt!r}")
