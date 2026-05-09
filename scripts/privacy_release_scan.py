"""Fail a release if tracked/public artifacts contain private material.

The default checks intentionally cover structural leaks only: real home
paths and email addresses. Per-release private terms from user prompts,
screenshots, terminal output, or local filenames must be supplied without
committing them, either via:

    SKYGREP_PRIVATE_PATTERNS='term one|term two'

or an untracked newline-delimited file:

    .release-private-patterns

The scan is for release gating, not runtime behavior.
"""

from __future__ import annotations

import html
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TARGETS = [
    "README.md",
    "LICENSE",
    "pyproject.toml",
    "AGENTS.md",
    "CLAUDE.md",
    ".github",
    "benchmarks",
    "docs",
    "skylakegrep",
    "tests",
]
SKIP_DIRS = {
    ".git",
    ".venv",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
    "target",
}
DEFAULT_PATTERNS = {
    "real macOS home path": re.compile(r"/Users/(?!example\b)[A-Za-z0-9._-]+"),
    "real macOS temp path": re.compile(r"/private/var/folders/[A-Za-z0-9._/-]+"),
    "real macOS var temp path": re.compile(r"/var/folders/[A-Za-z0-9._/-]+"),
    "email address": re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    ),
}


def _private_terms() -> list[str]:
    terms: list[str] = []
    env = os.environ.get("SKYGREP_PRIVATE_PATTERNS", "")
    if env:
        terms.extend(part.strip() for part in env.split("|") if part.strip())
    file_path = ROOT / ".release-private-patterns"
    if file_path.exists():
        terms.extend(
            line.strip()
            for line in file_path.read_text(errors="ignore").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    return terms


def _iter_files(paths: list[str]) -> Iterator[Path]:
    for raw in paths:
        path = (ROOT / raw).resolve()
        if not path.exists():
            continue
        if path.is_file():
            yield path
            continue
        for item in path.rglob("*"):
            if item.is_dir():
                continue
            try:
                rel_parts = item.relative_to(ROOT).parts
            except ValueError:
                rel_parts = item.parts
            if any(part in SKIP_DIRS for part in rel_parts):
                continue
            yield item


def _display_path(file_path: Path) -> str:
    try:
        return str(file_path.relative_to(ROOT))
    except ValueError:
        return str(file_path)


def _read_text(data: bytes) -> str:
    return data.decode("utf-8", errors="ignore")


def _iter_text_blobs(file_path: Path) -> Iterator[tuple[str, str]]:
    display = _display_path(file_path)
    suffixes = file_path.suffixes
    if file_path.suffix in {".whl", ".zip"}:
        try:
            with zipfile.ZipFile(file_path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    yield f"{display}!{info.filename}", _read_text(archive.read(info))
        except (OSError, zipfile.BadZipFile):
            return
        return

    if suffixes[-2:] in ([".tar", ".gz"], [".tar", ".bz2"], [".tar", ".xz"]):
        try:
            with tarfile.open(file_path) as archive:
                for member in archive.getmembers():
                    if not member.isfile():
                        continue
                    extracted = archive.extractfile(member)
                    if extracted is None:
                        continue
                    yield f"{display}!{member.name}", _read_text(extracted.read())
        except (OSError, tarfile.TarError):
            return
        return

    try:
        yield display, file_path.read_text(errors="ignore")
    except OSError:
        return


def _scan_variants(text: str) -> Iterator[tuple[str, str]]:
    yield "raw", text
    unescaped = html.unescape(text)
    if unescaped != text:
        yield "html-unescaped", unescaped


def main(argv: list[str]) -> int:
    paths = argv or DEFAULT_TARGETS
    patterns = dict(DEFAULT_PATTERNS)
    for idx, term in enumerate(_private_terms(), start=1):
        patterns[f"private term #{idx}"] = re.compile(re.escape(term), re.I)

    failures: list[str] = []
    for file_path in _iter_files(paths):
        for display, text in _iter_text_blobs(file_path):
            for variant_label, scan_text in _scan_variants(text):
                for label, pattern in patterns.items():
                    for match in pattern.finditer(scan_text):
                        line_no = scan_text.count("\n", 0, match.start()) + 1
                        failures.append(f"{display}:{line_no}:{variant_label}: {label}")

    if failures:
        print("privacy release scan failed:", file=sys.stderr)
        for item in failures[:200]:
            print(item, file=sys.stderr)
        if len(failures) > 200:
            print(f"... {len(failures) - 200} more", file=sys.stderr)
        return 1

    print("privacy release scan clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
