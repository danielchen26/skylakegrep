"""Terminal result rendering — colored, neat, hero-page-aligned.

The previous CLI output was plain ASCII (`=== path:lines (score: X.XXX) ===`)
inherited from v0.2.0. The website hero shows result cards with cyan paths,
right-aligned language pills, bold-green scores, and lightly syntax-
highlighted code. This module brings the terminal close to that visual,
purely with ANSI escapes (no extra dependency).

Color is auto-detected: applied only when stdout is a TTY and `NO_COLOR`
is not set in the environment. Pipe / redirect / `--json` paths get plain
text. The standard `NO_COLOR=1` opt-out is honoured.
"""

from __future__ import annotations

import os
import re
import shutil
import sys


# ---- ANSI primitives -----------------------------------------------

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_DIM = "\x1b[2m"

# Approximations of the website hero palette, mapped to the closest
# 256-color codes for terminals that don't speak truecolor.
_PATH_CYAN     = "\x1b[38;5;87m"   # ~#67e8f9
_LINE_DIM      = "\x1b[38;5;245m"  # dim grey for line ranges
_SCORE_GREEN   = "\x1b[38;5;77m"   # ~#34d399, bold
_PILL_TEXT     = "\x1b[38;5;87m"
_PILL_BG       = "\x1b[48;5;236m"  # dark grey background for the pill
_KW_AMBER      = "\x1b[38;5;215m"  # ~#fde68a — keywords (def, async, ...)
_FUNC_CYAN     = "\x1b[38;5;159m"  # ~#a5f3fc — function/class names
_TYPE_YELLOW   = "\x1b[38;5;229m"  # ~#fde68a — type annotations
_STR_GREEN     = "\x1b[38;5;156m"  # ~#86efac — strings
_NUM_AMBER     = "\x1b[38;5;221m"  # ~#fbbf24 — numbers / True / False
_COMMENT_DIM   = "\x1b[38;5;243m"  # dim grey — comments
_HEADER_RULE   = "\x1b[38;5;238m"  # very dim — separator rules


def _supports_color(stream=sys.stdout) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("MGREP_FORCE_COLOR"):
        return True
    return bool(getattr(stream, "isatty", lambda: False)())


# ---- Chunk metadata header ----------------------------------------
#
# The indexer prepends every stored chunk with a `[file: ...] [lang: ...]
# [symbol: ...]` line followed by a blank line. Useful for the embedder
# (gives each chunk anchoring tokens) but redundant in CLI output where
# the path and language already appear in the result header.

_META_RE = re.compile(
    r"^\[file:\s*(?P<file>[^\]]+)\]"
    r"(?:\s*\[lang:\s*(?P<lang>[^\]]+)\])?"
    r"(?:\s*\[symbol:\s*(?P<symbol>[^\]]+)\])?\s*$"
)


def _split_metadata(snippet: str) -> tuple[dict, str]:
    """Pull `[file:][lang:][symbol:]` from the snippet head; return
    (metadata_dict, body_without_header)."""
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
    while rest and not rest[0].strip():  # drop the blank separator line
        rest = rest[1:]
    return meta, "\n".join(rest)


# ---- Lightweight syntax highlighter --------------------------------
#
# Not pygments-grade, but covers the four-five token classes that show
# up in the hero mockup: keywords, def/class names, strings, comments,
# True/False/None. Works for Python / JS / TS / Rust / Go reasonably.

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


def _highlight_line(line: str) -> str:
    out = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        # Comment to EOL
        if ch == "#" or (ch == "/" and i + 1 < n and line[i + 1] == "/"):
            out.append(_COMMENT_DIM + line[i:] + _RESET)
            i = n
            continue
        # String literal — naive (handles "..." and '...' on one line)
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
        # Identifier / keyword
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (line[j].isalnum() or line[j] == "_"):
                j += 1
            tok = line[i:j]
            # Definition headers — keyword + name
            if tok in ("def", "class", "fn", "function", "func"):
                # Highlight keyword, then look for the next identifier
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
                # Type-ish (capitalised identifier) — yellow
                out.append(_TYPE_YELLOW + tok + _RESET)
            else:
                out.append(tok)
            i = j
            continue
        # Numeric literal
        if ch.isdigit():
            j = i
            while j < n and (line[j].isdigit() or line[j] in ".xX_eE"):
                j += 1
            out.append(_NUM_AMBER + line[i:j] + _RESET)
            i = j
            continue
        # Anything else (operators, whitespace) — passthrough
        out.append(ch)
        i += 1
    return "".join(out)


def _highlight(code: str) -> str:
    return "\n".join(_highlight_line(ln) for ln in code.split("\n"))


# ---- Public API ----------------------------------------------------


def _term_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default


def _shorten_path(path: str, project_root: str | None = None) -> str:
    """Show repo-relative if possible, else the raw path."""
    if project_root and path.startswith(project_root):
        rel = path[len(project_root):].lstrip("/")
        return rel or path
    return path


def render_terminal_result(
    r: dict,
    *,
    content: bool = True,
    max_chars: int = 500,
    color: bool | None = None,
    project_root: str | None = None,
) -> str:
    """Render one result card. Returns a string ready for click.echo."""
    use_color = _supports_color() if color is None else color
    width = _term_width()

    path = _shorten_path(r.get("path", "?"), project_root)
    line_range = ""
    if r.get("start_line") and r.get("end_line"):
        line_range = f":{r['start_line']}-{r['end_line']}"

    score = r.get("score", 0.0)
    snippet = r.get("snippet") or r.get("chunk") or ""
    meta, body = _split_metadata(snippet)
    lang = (r.get("language") or meta.get("lang") or "").strip()
    symbol = meta.get("symbol", "").strip()

    if use_color:
        path_part = f"{_PATH_CYAN}{path}{_RESET}{_LINE_DIM}{line_range}{_RESET}"
        score_part = f"{_BOLD}{_SCORE_GREEN}{score:.3f}{_RESET}"
        pill = (
            f"{_PILL_BG}{_PILL_TEXT} {lang or '·'} {_RESET}"
            if lang else ""
        )
        rule_top = f"{_HEADER_RULE}─{_RESET}"
    else:
        path_part = f"{path}{line_range}"
        score_part = f"{score:.3f}"
        pill = f"[{lang}]" if lang else ""
        rule_top = "─"

    # Layout: left=path, right=pill·score
    left = path_part
    right_plain = (f"[{lang}] " if lang else "") + f"{score:.3f}"
    pad = max(2, width - _visible_len(path + line_range) - len(right_plain) - 1)
    right = (pill + " " if pill else "") + score_part
    header = f"{left}{' ' * pad}{right}"

    out_lines = ["", header, rule_top * width]

    if symbol:
        if use_color:
            out_lines.append(f"{_DIM}symbol:{_RESET} {_FUNC_CYAN}{symbol}{_RESET}")
        else:
            out_lines.append(f"symbol: {symbol}")
        out_lines.append("")

    if content and body:
        body_clip = body[:max_chars]
        if use_color:
            body_clip = _highlight(body_clip)
        out_lines.append(body_clip)

    return "\n".join(out_lines)


def _visible_len(s: str) -> int:
    """Length of `s` ignoring ANSI escape sequences (we use this on
    plain strings here so it's just len, but kept abstracted in case
    callers pass already-coloured input)."""
    return len(re.sub(r"\x1b\[[0-9;]*m", "", s))


def render_compact_source(r: dict, *, color: bool | None = None) -> str:
    """One-line `- path:lines (score)` form used by the `--answer` Sources
    list. Lighter than the full result card but still coloured."""
    use_color = _supports_color() if color is None else color
    path = r.get("path", "?")
    line_range = ""
    if r.get("start_line") and r.get("end_line"):
        line_range = f":{r['start_line']}-{r['end_line']}"
    score = r.get("score", 0.0)
    if use_color:
        return (
            f"  {_PATH_CYAN}{path}{_RESET}"
            f"{_LINE_DIM}{line_range}{_RESET}  "
            f"{_BOLD}{_SCORE_GREEN}{score:.3f}{_RESET}"
        )
    return f"  {path}{line_range}  {score:.3f}"
