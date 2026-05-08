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

from skylakegrep.src import binary_extract
from skylakegrep.src.binary_extract import (
    ExtractedText,
    extract_docx,
    extract_pdf,
    extract_text,
    query_focused_passages,
    truncate,
)
from skylakegrep.src.render import render_terminal_result


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


def test_query_focused_passages_selects_relevant_original_text():
    text = (
        "CASE42 Project Report\n\n"
        "Deployment notes describe owners and timeline.\n\n"
        "Retry policy: use exponential backoff, cap attempts at three, "
        "and log the final failure reason.\n\n"
        "Audit notes describe unrelated approvals."
    )
    passages = query_focused_passages(
        text,
        "what does CASE42 say about retry policy",
        anchor="CASE42",
        max_passages=1,
    )
    assert passages
    assert "Retry policy" in passages[0]
    assert "exponential backoff" in passages[0]


def test_semantic_depth_summary_extracts_query_focused_filename_content(tmp_path):
    p = tmp_path / "CASE42_Project_Report.txt"
    p.write_text(
        "CASE42 Project Report\n\n"
        "Retry policy: use exponential backoff and cap attempts at three.\n\n"
        "Unrelated appendix text.\n",
        encoding="utf-8",
    )
    result = {
        "path": str(p),
        "file": str(p),
        "query": "what does CASE42_Project_Report say about retry policy",
        "chunk": "size: 0.1 KB    modified: now    type: txt",
        "snippet": "size: 0.1 KB    modified: now    type: txt",
        "language": "txt",
        "score": 1.0,
        "fallback": "filename-lookup",
        "filename_token": "CASE42_Project_Report",
        "_skygrep_semantic_depth": True,
    }
    out = render_terminal_result(result, detail="summary", color=False)
    assert "Retry policy" in out
    assert "exponential backoff" in out


def test_path_depth_summary_does_not_extract_filename_content(tmp_path):
    p = tmp_path / "CASE42_Project_Report.txt"
    p.write_text("Retry policy should stay hidden in path-depth summary.\n", encoding="utf-8")
    result = {
        "path": str(p),
        "file": str(p),
        "query": "where is CASE42_Project_Report file",
        "chunk": "size: 0.1 KB    modified: now    type: txt",
        "snippet": "size: 0.1 KB    modified: now    type: txt",
        "language": "txt",
        "score": 1.0,
        "fallback": "filename-lookup",
        "filename_token": "CASE42_Project_Report",
    }
    out = render_terminal_result(result, detail="summary", color=False)
    assert "size: 0.1 KB" in out
    assert "Retry policy should stay hidden" not in out


def test_semantic_filename_json_gets_query_focused_excerpt(tmp_path):
    from skylakegrep.src import cli as cli_module

    p = tmp_path / "CASE42_Project_Report.txt"
    p.write_text(
        "CASE42 Project Report\n\n"
        "Overview: release owners and timeline.\n\n"
        "Retry policy: use exponential backoff, cap attempts at three, "
        "and record the final failure reason.\n",
        encoding="utf-8",
    )
    result = {
        "path": str(p),
        "file": str(p),
        "query": "what does CASE42_Project_Report say about retry policy",
        "chunk": "size: 0.1 KB    modified: now    type: txt",
        "snippet": "size: 0.1 KB    modified: now    type: txt",
        "language": "txt",
        "score": 1.0,
        "fallback": "filename-lookup",
        "filename_token": "CASE42_Project_Report",
    }
    decision = cli_module.RouterDecision(
        intent="semantic",
        primary_token="CASE42_Project_Report",
        skip_cascade=False,
        skip_filename=False,
        skip_lexical=False,
        confidence=0.9,
        source="fast-intent",
        reason="semantic query with a concrete filename anchor",
        out_of_scope="none",
    )

    results = [result]
    cli_module._augment_filename_content_for_machine(
        results,
        "what does CASE42_Project_Report say about retry policy",
        decision,
        detail="summary",
        ocr=False,
    )

    assert results[0]["extracted_text_source"] == "text-passthrough"
    assert "Retry policy" in results[0]["content_excerpt"]
    assert "exponential backoff" in results[0]["query_excerpts"][0]


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


def test_extract_docx_word_lock_file_returns_friendly_hint(tmp_path):
    """v0.15.1 — `~$foo.docx` is a Word session lock file, not the
    real document. python-docx would emit 'Package not found' which
    is mysterious to users; we should detect and explain instead."""
    p = tmp_path / "~$Expert Letter.docx"
    p.write_bytes(b"random session metadata bytes")
    out = extract_docx(p)
    assert out.text == ""
    assert out.source == "lock-file"
    assert "lock" in out.note.lower()
    # Hint should suggest where the real doc is
    assert "Expert Letter.docx" in out.note


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
