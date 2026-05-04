"""Tests for the v0.15.0 binary content extractor.

Real PDF / docx fixtures are heavy to ship; we use:
  - On-the-fly pypdf-generated PDFs for PDF tests (no system dep)
  - python-docx generated docx for docx tests
  - Simple .txt files for the passthrough path

Pdftotext path is exercised opportunistically when poppler is on
PATH; otherwise pypdf fallback is verified.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from local_mgrep.src import binary_extract
from local_mgrep.src.binary_extract import (
    ExtractedText,
    extract_docx,
    extract_pdf,
    extract_text,
    truncate,
)


# ---- helpers --------------------------------------------------------


def _have_pypdf() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def _have_docx() -> bool:
    try:
        import docx  # noqa: F401
        return True
    except ImportError:
        return False


def _make_text_pdf(path: Path, text: str) -> None:
    """Create a real PDF with `text` as its body using pypdf."""
    from pypdf import PdfWriter
    from pypdf.generic import (
        ContentStream,
        DictionaryObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )
    # Easier path: use reportlab if available, fall back to a manually-
    # constructed minimal PDF stream. For test simplicity we'll embed
    # text as a content stream in a single-page doc.
    try:
        from reportlab.pdfgen import canvas  # noqa
        from reportlab.lib.pagesizes import letter
        c = canvas.Canvas(str(path), pagesize=letter)
        for i, line in enumerate(text.split("\n")):
            c.drawString(72, 720 - i * 14, line)
        c.save()
    except ImportError:
        # Minimal hand-rolled PDF — pypdf can read what it writes
        from pypdf import PdfWriter
        from pypdf.annotations import FreeText
        w = PdfWriter()
        w.add_blank_page(width=612, height=792)
        page = w.pages[0]
        annot = FreeText(
            text=text,
            rect=(72, 700, 540, 750),
            font="Helvetica",
            font_size="12pt",
        )
        w.add_annotation(page_number=0, annotation=annot)
        with open(path, "wb") as f:
            w.write(f)


def _make_docx(path: Path, text: str) -> None:
    from docx import Document
    doc = Document()
    for line in text.split("\n"):
        doc.add_paragraph(line)
    doc.save(str(path))


# ---- truncate -------------------------------------------------------


def test_truncate_short_text_unchanged():
    out, was_truncated = truncate("hello", 10)
    assert out == "hello"
    assert was_truncated is False


def test_truncate_long_text_clipped():
    out, was_truncated = truncate("a" * 100, 10)
    assert out.endswith(" …")
    assert was_truncated is True
    assert len(out) <= 12


# ---- extract_text dispatcher ---------------------------------------


def test_extract_text_missing_file(tmp_path):
    out = extract_text(tmp_path / "nope.pdf")
    assert out.text == ""
    assert out.source == "error"
    assert "not found" in out.note


def test_extract_text_unsupported_extension(tmp_path):
    p = tmp_path / "blob.xyz"
    p.write_bytes(b"\x00\x01\x02")
    out = extract_text(p)
    assert out.text == ""
    assert out.source == "no-extractor"


def test_extract_text_passthrough_for_plain_text(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("# Hello\n\nworld")
    out = extract_text(p)
    assert "Hello" in out.text
    assert out.source == "text-passthrough"


def test_extract_text_passthrough_csv(tmp_path):
    p = tmp_path / "data.csv"
    p.write_text("a,b,c\n1,2,3")
    out = extract_text(p)
    assert "1,2,3" in out.text
    assert out.source == "text-passthrough"


# ---- extract_pdf ---------------------------------------------------


@pytest.mark.skipif(not _have_pypdf(), reason="pypdf required")
def test_extract_pdf_empty_file_yields_no_text(tmp_path):
    """An empty PDF doesn't crash; it returns scanned-PDF hint."""
    from pypdf import PdfWriter
    p = tmp_path / "blank.pdf"
    w = PdfWriter()
    w.add_blank_page(width=612, height=792)
    with open(p, "wb") as f:
        w.write(f)

    out = extract_pdf(p)
    # Either pdftotext or pypdf produces empty text; both should set
    # has_text_layer=False and a scanned-PDF hint.
    assert out.has_text_layer is False
    assert out.source in ("pdftotext", "pypdf")
    assert "scanned" in out.note.lower() or "no text layer" in out.note.lower()


def test_extract_pdf_no_extractor_when_corrupt(tmp_path, monkeypatch):
    """If pypdf raises and pdftotext also fails, we get a clean
    no-extractor / error result instead of a crash."""
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"this is not a PDF")

    # Force shutil.which('pdftotext') to return None so we exercise
    # the pypdf path against a corrupt file.
    monkeypatch.setattr(
        binary_extract.shutil, "which",
        lambda name: None if name == "pdftotext" else "/bin/" + name,
    )
    out = extract_pdf(bad)
    # pypdf will fail to open → we return no-extractor since pdftotext
    # is also "missing" per our monkeypatch.
    assert out.text == ""
    assert out.source in ("no-extractor", "error", "pypdf")


# ---- extract_docx --------------------------------------------------


@pytest.mark.skipif(not _have_docx(), reason="python-docx required")
def test_extract_docx_round_trip(tmp_path):
    p = tmp_path / "doc.docx"
    _make_docx(p, "First paragraph.\nSecond paragraph.")
    out = extract_docx(p)
    assert "First paragraph" in out.text
    assert "Second paragraph" in out.text
    assert out.source == "python-docx"


@pytest.mark.skipif(not _have_docx(), reason="python-docx required")
def test_extract_docx_corrupt_returns_error(tmp_path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"not a docx")
    out = extract_docx(p)
    assert out.text == ""
    assert out.source == "error"


# ---- ExtractedText shape --------------------------------------------


def test_extracted_text_default_field_values():
    ex = ExtractedText(text="hello", source="test")
    assert ex.page_count == 0
    assert ex.has_text_layer is True
    assert ex.truncated is False
    assert ex.note == ""
