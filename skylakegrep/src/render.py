# SPDX-License-Identifier: Apache-2.0
"""Terminal result rendering — neat, hero-aligned, framed cards.

v0.13.0 redesigned this to match the website hero more closely:

  - Each result is wrapped in a proper rounded card frame
    ``╭─...─╮ │ body │ ╰─...─╯`` (left-bar variant, see below).
  - Code body is syntax-highlighted via Pygments
    ``Terminal256Formatter`` with a palette tuned to the hero —
    cyan paths, amber keywords, bright cyan function names, green
    strings, dim grey comments. Falls back to a hand-rolled
    highlighter on the rare environment without Pygments.
  - Different content types render appropriately. Code chunks pick
    their language from the indexer's ``language`` field. JSON
    snippets are pretty-printed when valid. Log / plain text passes
    through. Filename-lookup results show a metadata pill row
    (size · modified · type) instead of a body.

ANSI is auto-detected (TTY + ``NO_COLOR``); the standard opt-out is
honoured. Right-side card border is intentionally dropped so we
never need to compute padding around ANSI escape sequences.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys

from . import ui as ui_mod

# ---- Pygments (optional, soft-fallback) ---------------------------

try:
    from pygments import highlight as _pyg_highlight
    from pygments.formatters.terminal256 import Terminal256Formatter
    from pygments.lexers import get_lexer_by_name
    from pygments.styles import get_style_by_name
    from pygments.util import ClassNotFound

    _HAVE_PYGMENTS = True
except ImportError:  # pragma: no cover — pygments is in install_requires
    _HAVE_PYGMENTS = False


# ---- ANSI primitives -----------------------------------------------

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"

# 256-color approximations of the website hero palette
_PATH_CYAN = "\x1b[38;5;87m"     # ~#67e8f9
_LINE_DIM = "\x1b[38;5;245m"
_FRAME_DIM = "\x1b[38;5;239m"    # very dim — card frame
_SCORE_GREEN = "\x1b[38;5;77m"   # ~#34d399
_PILL_TEXT = "\x1b[38;5;87m"
_PILL_BG = "\x1b[48;5;236m"
_KW_AMBER = "\x1b[38;5;215m"
_FUNC_CYAN = "\x1b[38;5;159m"
_TYPE_YELLOW = "\x1b[38;5;229m"
_STR_GREEN = "\x1b[38;5;156m"
_NUM_AMBER = "\x1b[38;5;221m"
_COMMENT_DIM = "\x1b[38;5;243m"
_TIMESTAMP_DIM = "\x1b[38;5;242m"
_META_DIM = "\x1b[38;5;245m"


def _supports_color(stream=sys.stdout) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("SKYGREP_FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


# ---- Chunk metadata header strip ----------------------------------

_META_RE = re.compile(
    r"^\[file:\s*(?P<file>[^\]]+)\]"
    r"(?:\s*\[lang:\s*(?P<lang>[^\]]+)\])?"
    r"(?:\s*\[symbol:\s*(?P<symbol>[^\]]+)\])?\s*$"
)


def _split_metadata(snippet: str) -> tuple[dict, str]:
    if not snippet:
        return {}, snippet
    lines = snippet.split("\n")
    if not lines:
        return {}, snippet
    m = _META_RE.match(lines[0])
    if not m:
        return {}, snippet
    meta = {k: v for k, v in m.groupdict().items() if v}
    rest = lines[1:]
    while rest and not rest[0].strip():
        rest = rest[1:]
    return meta, "\n".join(rest)


# ---- Pygments-based highlighter -----------------------------------
#
# Pygments style closest to the hero palette is "monokai". We use the
# 256-color formatter so output renders correctly in any modern
# terminal without needing truecolor.

_PYG_FORMATTER = None
_PYG_STYLE_NAME = os.environ.get("SKYGREP_PYGMENTS_STYLE", "monokai")


def _get_formatter():
    global _PYG_FORMATTER
    if _PYG_FORMATTER is None and _HAVE_PYGMENTS:
        try:
            style = get_style_by_name(_PYG_STYLE_NAME)
        except ClassNotFound:
            style = get_style_by_name("monokai")
        _PYG_FORMATTER = Terminal256Formatter(style=style)
    return _PYG_FORMATTER


def _pyg_lex(code: str, lang: str | None) -> str:
    """Highlight ``code`` using Pygments. Returns the original on
    failure so a misdetected language never strips colour from the
    rest of the card."""
    if not _HAVE_PYGMENTS or not code:
        return code
    fmt = _get_formatter()
    if fmt is None:
        return code
    try:
        if lang:
            lexer = get_lexer_by_name(lang.lower(), stripnl=False, ensurenl=False)
        else:
            lexer = get_lexer_by_name("text", stripnl=False, ensurenl=False)
    except ClassNotFound:
        try:
            lexer = get_lexer_by_name("text", stripnl=False, ensurenl=False)
        except ClassNotFound:
            return code
    try:
        out = _pyg_highlight(code, lexer, fmt)
        # Pygments adds a trailing newline; trim to match input.
        if out.endswith("\n") and not code.endswith("\n"):
            out = out[:-1]
        return out
    except Exception:  # noqa: BLE001 — never let highlighting crash a search
        return code


# ---- JSON / log content-type renderers -----------------------------


def _looks_like_json(body: str) -> bool:
    s = body.strip()
    return bool(s) and s[0] in "{[" and s[-1] in "}]"


def _format_json(body: str) -> str:
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body
    return json.dumps(obj, indent=2, ensure_ascii=False)


_TIMESTAMP_RE = re.compile(
    r"^(\[?\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\]?)"
)


def _highlight_log(body: str, color: bool) -> str:
    if not color:
        return body
    out = []
    for line in body.split("\n"):
        m = _TIMESTAMP_RE.match(line)
        if m:
            out.append(
                f"{_TIMESTAMP_DIM}{m.group(1)}{_RESET}{line[m.end():]}"
            )
        else:
            out.append(line)
    return "\n".join(out)


# ---- Card frame ----------------------------------------------------


def _term_width(default: int = 100) -> int:
    raw = os.environ.get("SKYGREP_UI_WIDTH", "").strip()
    if raw:
        try:
            return min(max(60, int(raw)), 140)
        except ValueError:
            pass
    try:
        w = shutil.get_terminal_size((default, 24)).columns
    except OSError:
        w = default
    # cap so super-wide terminals don't produce comically long rules
    return min(max(60, w), 140)


def _shorten_path(path: str, project_root: str | None = None) -> str:
    if project_root:
        pr = project_root.rstrip("/")
        if path.startswith(pr + "/") or path == pr:
            rel = path[len(pr):].lstrip("/")
            return rel or path
    return path


def _visible_len(s: str) -> int:
    return len(re.sub(r"\x1b\[[0-9;]*m", "", s))


def _middle_ellipsize(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    keep_left = max(1, (max_len - 3) // 2)
    keep_right = max(1, max_len - 3 - keep_left)
    return f"{text[:keep_left]}...{text[-keep_right:]}"


def _wrap_plain_lines(text: str, width: int) -> str:
    if width <= 8:
        return text
    out: list[str] = []
    wrapper = None
    import textwrap

    wrapper = textwrap.TextWrapper(
        width=width,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=False,
        drop_whitespace=False,
    )
    for line in text.splitlines():
        if not line:
            out.append("")
            continue
        if len(line) <= width:
            out.append(line)
            continue
        out.extend(part.rstrip() for part in wrapper.wrap(line))
    return "\n".join(out)


# ---- Public API ----------------------------------------------------


def render_terminal_result(
    r: dict,
    *,
    content: bool = True,
    max_chars: int = 600,
    color: bool | None = None,
    project_root: str | None = None,
    detail: str = "standard",
    ocr: bool = False,
    explain: bool = False,
) -> str:
    """Render one result card. Returns a string ready for ``click.echo``.

    Layout:

        ╭─ path:lines ──...── pill  score
        │ symbol: foo                         (only when present)
        │
        │ <syntax-highlighted body>
        ╰────────────────────────────────────

    Each card uses a left-bar (``│``) on body lines plus a top
    ``╭─...`` and bottom ``╰─...`` rule. The right border is
    intentionally omitted so we don't have to compute pad-to-width
    around ANSI escape sequences (which would be lossy and brittle).
    """
    use_color = _supports_color() if color is None else color
    helix_rail = ui_mod.rail_style() == "helix"
    width = _term_width()

    raw_path = r.get("path", "?")
    path = _shorten_path(raw_path, project_root)
    line_range = ""
    if r.get("start_line") and r.get("end_line"):
        line_range = f":{r['start_line']}-{r['end_line']}"

    score = float(r.get("score") or 0.0)
    snippet = r.get("snippet") or r.get("chunk") or ""
    meta, body = _split_metadata(snippet)
    lang = (r.get("language") or meta.get("lang") or "").strip()
    symbol = meta.get("symbol", "").strip()
    fallback = r.get("fallback", "")

    # ----- Top rule with header -----
    pill_label = f"[{lang or fallback or 'file'}]" if (lang or fallback) else ""
    if helix_rail:
        plain_right = (
            f"{pill_label} score={score:.3f}"
            if pill_label else f"score={score:.3f}"
        )
    else:
        plain_right = (
            f" {pill_label}  {score:.3f}"
            if pill_label else f"  {score:.3f}"
        )
    width = ui_mod.available_content_columns(40) if helix_rail else width
    left_overhead = len("╭─ ")
    full_header = f"{path}{line_range}"
    # Reserve: path trailing space, at least one rule char, and the
    # separating space before the right-side pill/score.
    max_path_len = max(12, width - left_overhead - len(plain_right) - 3)
    path_header = _middle_ellipsize(full_header, max_path_len)

    if use_color:
        path_part = f"{_PATH_CYAN}{path_header}{_RESET}"
        score_part = f"{_BOLD}{_SCORE_GREEN}{score:.3f}{_RESET}"
        pill = (
            f"{_PILL_BG}{_PILL_TEXT} {lang or 'file'} {_RESET}"
            if (lang or fallback) else ""
        )
        corner_top = f"{_FRAME_DIM}╭─{_RESET}"
        corner_bottom = f"{_FRAME_DIM}╰{'─' * (width - 1)}{_RESET}"
        bar = f"{_FRAME_DIM}│ {_RESET}"
    else:
        path_part = path_header
        score_part = f"{score:.3f}"
        pill = f"[{lang or fallback or 'file'}]" if (lang or fallback) else ""
        corner_top = "╭─"
        corner_bottom = "╰" + "─" * (width - 1)
        bar = "│ "

    # Compose top: `╭─ <path:lines> ─...─ <pill> <score>`.
    # In helix mode the entire card lives inside the right content lane;
    # never let the terminal auto-wrap the score into the left workflow rail.
    plain_left = f"╭─ {path_header} "
    used = len(plain_left) + len(plain_right)
    fill = max(1, width - used - 1)
    if use_color:
        right = (
            f"{pill} score={score_part}"
            if helix_rail and pill
            else f"score={score_part}" if helix_rail
            else f"{pill}  {score_part}"
        )
        top = (
            f"{corner_top} {path_part} "
            f"{_FRAME_DIM}{'─' * fill}{_RESET} "
            f"{right}"
        )
    else:
        right = (
            f"{pill} score={score_part}"
            if helix_rail and pill
            else f"score={score_part}" if helix_rail
            else f"{pill}  {score_part}"
        )
        top = (
            f"{corner_top} {path_part} "
            f"{'─' * fill} "
            f"{right}"
        )

    out_lines: list[str] = ["", top]
    truncated_path_line = f"path: {full_header}" if path_header != full_header else ""

    # ----- v0.15.0 detail levels -----
    # brief   : header-only, no body / corner
    # summary : header + 1-line truncated body
    # standard: existing default
    # full    : standard + binary content extract for filename matches
    if detail == "brief":
        return ui_mod.block("\n".join(out_lines))

    # ----- Body -----
    body_lines: list[str] = []
    if truncated_path_line:
        body_lines.append(truncated_path_line)
        body_lines.append("")
    explain_line = (r.get("explain") or "").strip() if explain else ""
    has_meta = bool(symbol or explain_line)
    if symbol and detail != "summary":
        if use_color:
            body_lines.append(
                f"{_DIM}symbol:{_RESET} {_FUNC_CYAN}{symbol}{_RESET}"
            )
        else:
            body_lines.append(f"symbol: {symbol}")

    # 0.5.8 explainability: when --explain is on and the caller has
    # populated r["explain"] (built upstream in cli._build_explain_string
    # from signals already on the result dict — cosine_rank, symbol_rank,
    # symbol_channel_terms, fallback, score), render a one-line "via:"
    # under the symbol so the user sees WHY this chunk was returned.
    if explain_line and detail != "summary":
        if use_color:
            body_lines.append(f"{_DIM}via:{_RESET} {_DIM}{explain_line}{_RESET}")
        else:
            body_lines.append(f"via: {explain_line}")

    if has_meta and detail != "summary":
        body_lines.append("")

    # Lazy binary-content extraction for filename-lookup results.
    # PDFs and docx are not in the chunk index, so query-depth document
    # evidence must be extracted on demand. For semantic-depth queries,
    # show query-focused raw passages first; for pure path lookups,
    # keep extraction opt-in via `--detail=full`.
    extracted_preview = ""
    semantic_depth = bool(r.get("_skygrep_semantic_depth"))
    query_text = str(r.get("query") or "").strip()
    should_extract_binary = (
        fallback == "filename-lookup"
        and (
            detail == "full"
            or (semantic_depth and detail in {"summary", "standard"})
        )
    )
    if should_extract_binary:
        from . import binary_extract
        from pathlib import Path as _P
        try:
            ex = binary_extract.extract_text(_P(raw_path), ocr=ocr)
            anchor = str(r.get("filename_token") or _P(raw_path).stem)
            focused: list[str] = []
            if query_text:
                focused = binary_extract.query_focused_passages(
                    ex.text,
                    query_text,
                    anchor=anchor,
                    max_passages=1 if detail == "summary" else 2,
                    max_chars=220 if detail == "summary" else 900,
                )
            preview = ""
            was_truncated = False
            if detail == "full":
                preview, was_truncated = binary_extract.truncate(ex.text, 1200)
            elif not focused:
                preview, was_truncated = binary_extract.truncate(ex.text, 220)
            if focused:
                label = f"{ex.source} · query excerpts"
                extracted_preview = (
                    f"{_DIM}[{label}]{_RESET}\n"
                    if use_color else f"[{label}]\n"
                )
                extracted_preview += "\n\n".join(focused)
                if detail == "full" and preview:
                    extracted_preview += (
                        f"\n\n{_DIM}[{ex.source} · full preview"
                        f"{' · truncated' if was_truncated else ''}]{_RESET}\n"
                        if use_color
                        else f"\n\n[{ex.source} · full preview"
                        f"{' · truncated' if was_truncated else ''}]\n"
                    )
                    extracted_preview += preview
            elif preview:
                label = f"{ex.source}{' · truncated' if was_truncated else ''}"
                extracted_preview = (
                    f"{_DIM}[{label}]{_RESET}\n"
                    if use_color else f"[{label}]\n"
                )
                extracted_preview += preview
            elif ex.note:
                # Surface the friendly hint (e.g. "scanned PDF, rerun
                # with --ocr") so the user sees WHY there's no body.
                hint_color = _DIM if use_color else ""
                reset = _RESET if use_color else ""
                extracted_preview = f"{hint_color}{ex.note}{reset}"
        except Exception:  # noqa: BLE001 — never let extraction crash a search
            pass

    if extracted_preview:
        if helix_rail and "\x1b[" not in extracted_preview:
            body_width = width - _visible_len(bar)
            extracted_preview = _wrap_plain_lines(extracted_preview, body_width)
        body_lines.extend(extracted_preview.split("\n"))
        body_lines.append("")

    if content and body:
        body_width = width - _visible_len(bar)
        if detail == "summary":
            # One-line preview only
            first_line = next(
                (ln for ln in body.split("\n") if ln.strip()), ""
            )
            summary_clip = first_line[:160].rstrip()
            if helix_rail:
                summary_clip = _wrap_plain_lines(summary_clip, body_width)
            if summary_clip:
                rendered = _render_body_by_type(
                    summary_clip,
                    lang=lang,
                    fallback=fallback,
                    color=use_color,
                )
                body_lines.append(rendered)
        else:
            body_clip = body[:max_chars]
            if helix_rail:
                body_clip = _wrap_plain_lines(body_clip, body_width)
            rendered = _render_body_by_type(
                body_clip,
                lang=lang,
                fallback=fallback,
                color=use_color,
            )
            body_lines.extend(rendered.split("\n"))

    if not body_lines:
        # Even with no body, keep the card visible so there's always a
        # bottom rule to match the top — looks consistent.
        body_lines.append("")

    for ln in body_lines:
        out_lines.append(f"{bar}{ln}")

    if corner_bottom:
        out_lines.append(corner_bottom)
    return ui_mod.block("\n".join(out_lines))


def _render_body_by_type(
    body: str,
    *,
    lang: str,
    fallback: str,
    color: bool,
) -> str:
    """Dispatch to the right renderer based on content type."""
    if not color:
        # Plain mode — return body as-is (or pretty-print JSON, which
        # helps even without colour).
        if _looks_like_json(body):
            return _format_json(body)
        return body

    # Filename-lookup result: a single metadata line, dim cyan
    if fallback == "filename-lookup":
        return f"{_META_DIM}{body}{_RESET}"

    # JSON content
    if lang in ("json", "jsonl") or _looks_like_json(body):
        formatted = _format_json(body) if _looks_like_json(body) else body
        return _pyg_lex(formatted, "json")

    # Log files — dim timestamps, otherwise plain
    if lang in ("log",):
        return _highlight_log(body, color)

    # Markdown
    if lang in ("md", "markdown"):
        return _pyg_lex(body, "md")

    # Code (default) — Pygments by language, fallback to plain
    if _HAVE_PYGMENTS:
        return _pyg_lex(body, lang or None)

    # No Pygments — last-resort hand-rolled
    return _hand_highlight(body)


# ---- Fallback hand-rolled highlighter (no Pygments) ----------------

_KEYWORDS = {
    "def", "class", "return", "if", "else", "elif", "for", "while",
    "import", "from", "as", "with", "try", "except", "finally", "raise",
    "yield", "async", "await", "lambda", "pass", "break", "continue",
    "in", "is", "not", "and", "or", "global", "nonlocal",
    "fn", "let", "const", "var", "function", "interface", "type",
    "struct", "enum", "impl", "trait", "pub", "use", "match", "mod",
    "package", "func", "go", "defer", "select", "case", "default",
    "switch",
}
_LITERALS = {"True", "False", "None", "true", "false", "null", "nil", "undefined"}


def _hand_highlight_line(line: str) -> str:
    out = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "#" or (ch == "/" and i + 1 < n and line[i + 1] == "/"):
            out.append(_COMMENT_DIM + line[i:] + _RESET)
            i = n
            continue
        if ch in ('"', "'"):
            quote = ch
            j = i + 1
            while j < n and line[j] != quote:
                if line[j] == "\\" and j + 1 < n:
                    j += 2
                else:
                    j += 1
            j = min(j + 1, n)
            out.append(_STR_GREEN + line[i:j] + _RESET)
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (line[j].isalnum() or line[j] == "_"):
                j += 1
            tok = line[i:j]
            if tok in ("def", "class", "fn", "function", "func"):
                k = j
                while k < n and line[k] == " ":
                    k += 1
                m = k
                while m < n and (line[m].isalnum() or line[m] == "_"):
                    m += 1
                if m > k:
                    out.append(_KW_AMBER + tok + _RESET)
                    out.append(line[j:k])
                    out.append(_FUNC_CYAN + line[k:m] + _RESET)
                    i = m
                    continue
            if tok in _KEYWORDS:
                out.append(_KW_AMBER + tok + _RESET)
            elif tok in _LITERALS:
                out.append(_NUM_AMBER + tok + _RESET)
            elif tok and tok[0].isupper():
                out.append(_TYPE_YELLOW + tok + _RESET)
            else:
                out.append(tok)
            i = j
            continue
        if ch.isdigit():
            j = i
            while j < n and (line[j].isdigit() or line[j] in ".xX_eE"):
                j += 1
            out.append(_NUM_AMBER + line[i:j] + _RESET)
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _hand_highlight(code: str) -> str:
    return "\n".join(_hand_highlight_line(ln) for ln in code.split("\n"))


def render_compact_source(r: dict, *, color: bool | None = None) -> str:
    """One-line `- path:lines (score)` form used by the `--answer`
    Sources list."""
    use_color = _supports_color() if color is None else color
    path = r.get("path", "?")
    line_range = ""
    if r.get("start_line") and r.get("end_line"):
        line_range = f":{r['start_line']}-{r['end_line']}"
    score = float(r.get("score") or 0.0)
    if use_color:
        return (
            f"  {_PATH_CYAN}{path}{_RESET}"
            f"{_LINE_DIM}{line_range}{_RESET}  "
            f"{_BOLD}{_SCORE_GREEN}{score:.3f}{_RESET}"
        )
    return f"  {path}{line_range}  {score:.3f}"
