# SPDX-License-Identifier: Apache-2.0
"""The release privacy scan reads image metadata, not compressed pixel data."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "privacy_release_scan", Path(__file__).resolve().parent.parent / "scripts" / "privacy_release_scan.py"
)
scan = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(scan)


# Built at runtime so this file itself never matches the scan patterns.
AT = b"@"
EMAIL = b"someone" + AT + b"example.org"
LOOKALIKE = b"Z." + AT + b"Q.Hgvd"
HOME = b"/Users/" + b"realname/shot.png"


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _png(text_chunk: bytes | None, pixel_bytes: bytes) -> bytes:
    out = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    if text_chunk is not None:
        out += _png_chunk(b"tEXt", text_chunk)
    return out + _png_chunk(b"IDAT", pixel_bytes) + _png_chunk(b"IEND", b"")


def _gif(comment: bytes | None, pixel_bytes: bytes) -> bytes:
    out = b"GIF89a" + struct.pack("<HHBBB", 1, 1, 0x80, 0, 0) + b"\x00\x00\x00\xff\xff\xff"
    if comment is not None:
        out += b"\x21\xfe" + bytes([len(comment)]) + comment + b"\x00"
    out += b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0) + b"\x02"
    out += bytes([len(pixel_bytes)]) + pixel_bytes + b"\x00"
    return out + b"\x3b"


class ImageMetadataScanTests(unittest.TestCase):
    def _run(self, name: str, data: bytes) -> int:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / name
            path.write_bytes(data)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return scan.main([str(path)])

    def test_pixel_bytes_that_look_like_an_email_are_ignored(self):
        self.assertEqual(self._run("a.png", _png(None, b"\x00" + LOOKALIKE + b"\x00")), 0)
        self.assertEqual(self._run("a.gif", _gif(None, LOOKALIKE)), 0)

    def test_png_text_chunk_is_scanned(self):
        self.assertEqual(self._run("a.png", _png(b"Author\x00" + EMAIL, b"\x00")), 1)
        self.assertEqual(self._run("b.png", _png(b"Source\x00" + HOME, b"\x00")), 1)

    def test_gif_comment_is_scanned(self):
        self.assertEqual(self._run("a.gif", _gif(b"made by " + EMAIL, b"\x00")), 1)

    def test_unparseable_image_falls_back_to_raw_scan(self):
        self.assertEqual(self._run("broken.png", b"not a png " + EMAIL), 1)

    def test_repository_images_parse(self):
        root = Path(__file__).resolve().parent.parent / "docs" / "assets"
        for path in list(root.rglob("*.png")) + list(root.rglob("*.gif")):
            data = path.read_bytes()
            reader = scan._png_text if path.suffix == ".png" else scan._gif_text
            reader(data)  # raises if the parser cannot walk the file


if __name__ == "__main__":
    unittest.main()
