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
import struct
import sys
import tarfile
import zipfile
import zlib
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


def _png_text(data: bytes) -> str:
    """Text a PNG can carry: tEXt / zTXt / iTXt chunks (and nothing from the
    compressed pixel stream, which only produces false positives)."""

    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a PNG")
    out: list[str] = []
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        if len(body) != length:
            raise ValueError("truncated PNG chunk")
        if kind == b"tEXt":
            out.append(body.decode("latin-1"))
        elif kind == b"zTXt":
            key, _, rest = body.partition(b"\0")
            out.append(key.decode("latin-1") + " " + zlib.decompress(rest[1:]).decode("latin-1"))
        elif kind == b"iTXt":
            key, _, rest = body.partition(b"\0")
            compressed = rest[:1] == b"\x01"
            _, _, rest = rest[2:].partition(b"\0")      # language tag
            translated, _, text = rest.partition(b"\0")
            if compressed:
                text = zlib.decompress(text)
            out.append(" ".join(x.decode("utf-8", "ignore") for x in (key, translated, text)))
        pos += 12 + length
        if kind == b"IEND":
            break
    return "\n".join(out)


def _gif_text(data: bytes) -> str:
    """Text a GIF can carry: comment, plain-text and application extensions
    (skipping LZW image data, which only produces false positives)."""

    if data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("not a GIF")
    out: list[str] = []
    pos = 13
    flags = data[10]
    if flags & 0x80:
        pos += 3 * (2 ** ((flags & 0x07) + 1))

    def sub_blocks(i: int) -> tuple[bytes, int]:
        chunks = []
        while True:
            if i >= len(data):
                raise ValueError("truncated GIF")
            size = data[i]
            i += 1
            if size == 0:
                return b"".join(chunks), i
            chunks.append(data[i : i + size])
            i += size

    while pos < len(data):
        marker = data[pos]
        if marker == 0x3B:                      # trailer
            break
        if marker == 0x21:                      # extension
            label = data[pos + 1]
            if label == 0x01:                   # plain text: 12-byte header, then text sub-blocks
                pos += 2 + 1 + data[pos + 2]
            else:
                pos += 2
            payload, pos = sub_blocks(pos)
            if label in (0x01, 0xFE, 0xFF):
                out.append(payload.decode("utf-8", "ignore"))
        elif marker == 0x2C:                    # image descriptor
            lflags = data[pos + 9]
            pos += 10
            if lflags & 0x80:
                pos += 3 * (2 ** ((lflags & 0x07) + 1))
            pos += 1                            # LZW minimum code size
            _, pos = sub_blocks(pos)
        else:
            raise ValueError(f"unexpected GIF block 0x{marker:02x}")
    return "\n".join(out)


_METADATA_READERS = {".png": _png_text, ".gif": _gif_text}


def _iter_text_blobs(file_path: Path) -> Iterator[tuple[str, str]]:
    display = _display_path(file_path)
    suffixes = file_path.suffixes
    reader = _METADATA_READERS.get(file_path.suffix.lower())
    if reader is not None:
        try:
            data = file_path.read_bytes()
        except OSError:
            return
        try:
            yield display, reader(data)
        except (ValueError, IndexError, struct.error, zlib.error):
            # Unparseable image: fall back to the conservative raw scan.
            yield display, _read_text(data)
        return
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
