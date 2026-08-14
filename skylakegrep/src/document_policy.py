"""Generic document-authority rules shared by retrieval and synthesis."""

from __future__ import annotations

import re
from pathlib import Path


_VERSION_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])v?(\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?)",
    re.IGNORECASE,
)
_DOCUMENT_SUFFIXES = {".html", ".md", ".rst", ".txt"}
_LIVING_AUTHORITY_TOKENS = {
    "CONTRIBUTING",
    "GUIDE",
    "POLICY",
    "README",
    "REFERENCE",
    "RELEASE",
    "RELEASING",
    "RUNBOOK",
    "SECURITY",
}


def _version_tokens(value: str) -> set[str]:
    return {
        match.group(1).lower()
        for match in _VERSION_TOKEN_RE.finditer(value)
    }


def is_document_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    return (
        normalized.startswith("docs/")
        or "/docs/" in normalized
        or Path(normalized).suffix in _DOCUMENT_SUFFIXES
    )


def is_unnamed_version_snapshot(query: str, path: str) -> bool:
    """Whether ``path`` is a versioned document not named by ``query``."""

    if not is_document_path(path):
        return False
    path_versions = _version_tokens(Path(path).name)
    if not path_versions:
        return False
    return not bool(path_versions & _version_tokens(query))


def is_living_authority_document(path: str) -> bool:
    """Recognize unversioned policy/reference documents by generic metadata."""

    if not is_document_path(path) or _version_tokens(Path(path).name):
        return False
    stem_tokens = {
        token
        for token in re.split(r"[^A-Za-z]+", Path(path).stem.upper())
        if token
    }
    return bool(stem_tokens & _LIVING_AUTHORITY_TOKENS)


def _query_names_document(query: str, path: str) -> bool:
    query_tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z0-9]+", query)
        if len(token) >= 4
    }
    stem_tokens = {
        token.lower()
        for token in re.split(r"[^A-Za-z0-9]+", Path(path).stem)
        if len(token) >= 4
    }
    return bool(query_tokens & stem_tokens)


def prefer_living_authority_results(query: str, results: list[dict]) -> list[dict]:
    """Exclude stale snapshots when the leading evidence is a living authority.

    Retrieval has already ranked the result set. This policy only activates
    when that ranking places an unversioned policy/reference document first.
    At that point unrelated plans, roadmaps, and historical notes are not
    authoritative answer evidence, so retain only living authorities,
    explicitly named documents/versions, and non-document source evidence.
    Historical/changelog tasks without a leading living authority remain
    unchanged.
    """

    if not results:
        return results
    leading_path = str(results[0].get("path") or results[0].get("file") or "")
    if not is_living_authority_document(leading_path):
        return results
    filtered = []
    for result in results:
        path = str(result.get("path") or result.get("file") or "")
        if not is_document_path(path):
            filtered.append(result)
            continue
        if is_living_authority_document(path):
            filtered.append(result)
            continue
        path_versions = _version_tokens(Path(path).name)
        if path_versions:
            if not is_unnamed_version_snapshot(query, path):
                filtered.append(result)
            continue
        if _query_names_document(query, path):
            filtered.append(result)
    return filtered or results
