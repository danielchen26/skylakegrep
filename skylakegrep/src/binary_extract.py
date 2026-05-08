"""Lazy text extraction from binary files (PDF / docx / scanned PDF
via OCR). Used by the v0.15.0 render layer to surface CONTENT preview
on filename-lookup matches — so a query like ``where is task-001 file?``
returns the filename match plus a snippet of the document body, not
just the filename + size.

Design contract:

  - **Lazy**: only called for the top-K results in the rendered list,
    never bulk-extract every file in a directory.
  - **Fast path first**: ``pdftotext`` (poppler) shells out and is
    typically <50 ms / page. ``pypdf`` is the pure-Python fallback
    when poppler isn't on PATH (e.g. CI).
  - **Scanned PDF detection**: if the text-layer extraction returns
    < 100 characters, we mark ``has_text_layer=False`` and the user
    sees a message suggesting ``--ocr`` (which routes through
    ``tesseract``, opt-in only because OCR is slow: ~5-30 s / page).
  - **Failure modes are visible**: every failure yields an empty
    body and a ``source`` field that names the failure cause
    (``"empty"``, ``"no-extractor"``, ``"timeout"``, ``"error"``).
    The render layer can decide to display a friendly hint in
    place of the missing body.
"""

from __future__ import annotations

import logging
import math
import re
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ---- Public dataclass ----------------------------------------------


@dataclass
class ExtractedText:
    text: str
    source: str          # "pdftotext" | "pypdf" | "python-docx" | "tesseract" | "empty" | "no-extractor" | "timeout" | "error"
    page_count: int = 0
    has_text_layer: bool = True
    truncated: bool = False
    note: str = ""       # human-friendly hint (e.g. "scanned PDF; rerun with --ocr")


# ---- PDF extraction ------------------------------------------------


_PDFTOTEXT_TIMEOUT = 8  # seconds — generous; most PDFs finish in <1 s


def _extract_pdf_pdftotext(path: Path) -> ExtractedText | None:
    """First-choice PDF extractor: shell out to ``pdftotext`` (from
    poppler). Returns ``None`` if poppler is not on PATH so the caller
    can try the pypdf fallback."""
    if not shutil.which("pdftotext"):
        return None
    try:
        r = subprocess.run(
            ["pdftotext", "-layout", "-q", str(path), "-"],
            capture_output=True,
            text=True,
            timeout=_PDFTOTEXT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return ExtractedText(
            text="",
            source="timeout",
            note=f"pdftotext exceeded {_PDFTOTEXT_TIMEOUT}s on {path.name}",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("pdftotext failed on %s: %s", path, exc)
        return None
    body = (r.stdout or "").strip()
    return ExtractedText(
        text=body,
        source="pdftotext",
        has_text_layer=len(body) >= 100,
        note=("scanned PDF (no text layer); rerun with --ocr for tesseract"
              if len(body) < 100 else ""),
    )


def _extract_pdf_pypdf(path: Path) -> ExtractedText | None:
    """Fallback: pure-Python ``pypdf``. Slower but no system dep."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.debug("pypdf open failed on %s: %s", path, exc)
        return None
    parts: list[str] = []
    page_count = 0
    for page in reader.pages:
        page_count += 1
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
    body = "\n\n".join(p for p in parts if p).strip()
    return ExtractedText(
        text=body,
        source="pypdf",
        page_count=page_count,
        has_text_layer=len(body) >= 100,
        note=("scanned PDF (no text layer); rerun with --ocr for tesseract"
              if len(body) < 100 else ""),
    )


def _extract_pdf_ocr(path: Path) -> ExtractedText:
    """OCR via ``tesseract`` (opt-in only — slow, 5-30 s / page).
    Requires both ``pdftoppm`` (poppler) to rasterise pages and
    ``tesseract`` to OCR the resulting images."""
    if not shutil.which("tesseract"):
        return ExtractedText(
            text="",
            source="no-extractor",
            note="tesseract not on PATH; install via `brew install tesseract`",
        )
    if not shutil.which("pdftoppm"):
        return ExtractedText(
            text="",
            source="no-extractor",
            note="pdftoppm (poppler) not on PATH; required to rasterise PDF for OCR",
        )
    # Rasterise + OCR is heavy. We delegate to ``pdftoppm | tesseract``
    # via subprocess pipeline. For the v0.15.0 first cut we OCR the
    # entire PDF; later we can support page ranges.
    try:
        r1 = subprocess.run(
            ["pdftoppm", "-r", "200", str(path), "-"],
            capture_output=True,
            timeout=300,
        )
        r2 = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", "eng"],
            input=r1.stdout,
            capture_output=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return ExtractedText(
            text="", source="timeout",
            note="OCR exceeded 5 minutes; PDF may be very long",
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ExtractedText(
            text="", source="error",
            note=f"OCR pipeline failed: {exc}",
        )
    body = (r2.stdout.decode("utf-8", errors="ignore") or "").strip()
    return ExtractedText(
        text=body,
        source="tesseract",
        has_text_layer=False,
        note="extracted via tesseract OCR",
    )


def extract_pdf(path: Path, *, ocr: bool = False) -> ExtractedText:
    """Try pdftotext → pypdf → (optional OCR) and return the first
    non-empty result. ``ocr=True`` forces tesseract pipeline regardless
    of whether a text layer exists; ``ocr=False`` (default) attempts
    text-layer extraction only and surfaces a hint when the PDF is
    scanned."""
    if ocr:
        return _extract_pdf_ocr(path)
    for extractor in (_extract_pdf_pdftotext, _extract_pdf_pypdf):
        result = extractor(path)
        if result is None:
            continue
        if result.text or result.source in ("timeout", "error"):
            return result
        # Empty extraction with a real extractor → keep the result so
        # we surface the "scanned PDF" hint to the user.
        return result
    return ExtractedText(
        text="",
        source="no-extractor",
        note="no PDF extractor available; install `poppler` or `pip install pypdf`",
    )


# ---- DOCX extraction -----------------------------------------------


def extract_docx(path: Path) -> ExtractedText:
    """Extract text from a .docx via python-docx."""
    # MS Word session-lock files start with `~$` and are tiny
    # (~0.2 KB session metadata, NOT OOXML zip). python-docx fails
    # with the unhelpful "Package not found" — surface a friendly
    # hint instead so the user understands this isn't the real doc.
    if path.name.startswith("~$"):
        return ExtractedText(
            text="",
            source="lock-file",
            note=(
                "Word session lock file (not the actual document); "
                f"the real doc is likely '{path.name[2:]}' or similar"
            ),
        )
    try:
        from docx import Document
    except ImportError:
        return ExtractedText(
            text="",
            source="no-extractor",
            note="python-docx not installed; `pip install python-docx`",
        )
    try:
        doc = Document(str(path))
    except Exception as exc:  # noqa: BLE001
        logger.debug("docx open failed on %s: %s", path, exc)
        return ExtractedText(
            text="", source="error",
            note=f"docx parser failed: {exc}",
        )
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    body = "\n".join(parts).strip()
    return ExtractedText(
        text=body,
        source="python-docx",
        has_text_layer=len(body) >= 50,
    )


# ---- Dispatcher ----------------------------------------------------


def extract_text(path: Path | str, *, ocr: bool = False) -> ExtractedText:
    """Dispatch to the right extractor by file extension. Returns an
    empty ``ExtractedText`` with a descriptive ``source`` for any
    unsupported type — the render layer can decide to display a hint
    or just suppress the body."""
    p = Path(path)
    if not p.exists():
        return ExtractedText(text="", source="error", note="file not found")
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf(p, ocr=ocr)
    if suffix == ".docx":
        return extract_docx(p)
    if suffix in (".txt", ".md", ".rst", ".log", ".csv", ".tsv"):
        try:
            return ExtractedText(
                text=p.read_text(errors="ignore").strip(),
                source="text-passthrough",
            )
        except OSError as exc:
            return ExtractedText(
                text="", source="error", note=f"read failed: {exc}",
            )
    return ExtractedText(
        text="",
        source="no-extractor",
        note=f"no extractor for *{suffix} (binary or unsupported)",
    )


def truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate text to ``max_chars``. Returns (truncated_text, was_truncated)."""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars].rstrip() + " …", True


_QUERY_WORD_RE = re.compile(r"[A-Za-z0-9._-]{2,80}|[\u3400-\u9fff]{2,32}")


def _features(text: str) -> Counter[str]:
    """Generic char/word n-gram features for query-focused excerpts.

    This is intentionally not a phrase/keyword list. It lets PDF/DOCX
    rendering find the paragraph most related to the user's question
    without invoking an LLM or hard-coding language wrappers.
    """

    text = (text or "").strip().casefold()
    compact = "".join(ch for ch in text if not ch.isspace())
    out: Counter[str] = Counter()
    for n, weight in ((2, 1), (3, 2), (4, 2)):
        for i in range(max(0, len(compact) - n + 1)):
            out[f"c:{compact[i:i + n]}"] += weight
    for word in _QUERY_WORD_RE.findall(text):
        out[f"w:{word}"] += 4
    return out


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    an = math.sqrt(sum(v * v for v in a.values()))
    bn = math.sqrt(sum(v * v for v in b.values()))
    if not an or not bn:
        return 0.0
    return dot / (an * bn)


def _query_focus(query: str, anchor: str = "") -> str:
    """Drop the filename/path anchor from the query before excerpt scoring."""

    focus = query or ""
    anchors = {anchor, Path(anchor).stem if anchor else ""}
    for item in list(anchors):
        if not item:
            continue
        variants = {
            item,
            item.replace("_", " "),
            item.replace("-", " "),
            item.replace(".", " "),
        }
        for variant in variants:
            if variant:
                focus = re.sub(re.escape(variant), " ", focus, flags=re.I)
    focus = " ".join(focus.split())
    return focus or query or ""


def _passage_units(text: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text or "") if p.strip()]
    if len(paragraphs) > 1:
        return paragraphs
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    return lines


def query_focused_passages(
    text: str,
    query: str,
    *,
    anchor: str = "",
    max_passages: int = 2,
    max_chars: int = 900,
) -> list[str]:
    """Return raw passages most related to ``query``.

    The result is evidence, not a synthetic summary: the render layer
    shows original PDF/DOCX/TXT text selected by a generic n-gram
    similarity score. If no passage has any useful similarity, fall
    back to the first non-empty passage so the user still sees content.
    """

    units = _passage_units(text)
    if not units:
        return []
    qv = _features(_query_focus(query, anchor=anchor))
    scored = [(_cosine(qv, _features(unit)), idx, unit) for idx, unit in enumerate(units)]
    scored.sort(key=lambda item: (-item[0], item[1]))
    if scored[0][0] <= 0.0:
        selected = [units[0]]
    else:
        selected = [unit for score, _, unit in scored if score > 0.0][:max_passages]

    out: list[str] = []
    used = 0
    for unit in selected:
        remaining = max_chars - used
        if remaining <= 0:
            break
        clipped, _ = truncate(unit, remaining)
        if clipped:
            out.append(clipped)
            used += len(clipped)
    return out
