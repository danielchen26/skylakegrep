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
    if os.environ.get("MGREP_FORCE_COLOR"):
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
_PYG_STYLE_NAME = os.environ.get("MGREP_PYGMENTS_STYLE", "monokai")


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


# ---- Public API ----------------------------------------------------


def render_terminal_result(
    r: dict,
    *,
    content: bool = True,
    max_chars: int = 600,
    color: bool | None = None,
    project_root: str | None = None,
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
    if use_color:
        path_part = f"{_PATH_CYAN}{path}{_RESET}{_LINE_DIM}{line_range}{_RESET}"
        score_part = f"{_BOLD}{_SCORE_GREEN}{score:.3f}{_RESET}"
        pill = (
            f"{_PILL_BG}{_PILL_TEXT} {lang or 'file'} {_RESET}"
            if (lang or fallback) else ""
        )
        corner_top = f"{_FRAME_DIM}╭─{_RESET}"
        corner_bottom = f"{_FRAME_DIM}╰{'─' * (width - 1)}{_RESET}"
        bar = f"{_FRAME_DIM}│ {_RESET}"
    else:
        path_part = f"{path}{line_range}"
        score_part = f"{score:.3f}"
        pill = f"[{lang or fallback or 'file'}]" if (lang or fallback) else ""
        corner_top = "╭─"
        corner_bottom = "╰" + "─" * (width - 1)
        bar = "│ "

    # Compose top: `╭─ <path:lines> ─...─ <pill> <score>`
    plain_left = f"╭─ {path}{line_range} "
    plain_right = (f" {('[' + (lang or fallback or 'file') + ']')} {score:.3f}")
    used = len(plain_left) + len(plain_right)
    fill = max(2, width - used)
    if use_color:
        top = (
            f"{corner_top} {path_part} "
            f"{_FRAME_DIM}{'─' * fill}{_RESET} "
            f"{pill}  {score_part}"
        )
    else:
        top = (
            f"{corner_top} {path}{line_range} "
            f"{'─' * fill} "
            f"{pill}  {score:.3f}"
        )

    out_lines: list[str] = ["", top]

    # ----- Body -----
    body_lines: list[str] = []
    if symbol:
        if use_color:
            body_lines.append(
                f"{_DIM}symbol:{_RESET} {_FUNC_CYAN}{symbol}{_RESET}"
            )
        else:
            body_lines.append(f"symbol: {symbol}")
        body_lines.append("")

    if content and body:
        body_clip = body[:max_chars]
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

    out_lines.append(corner_bottom)
    return "\n".join(out_lines)


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
