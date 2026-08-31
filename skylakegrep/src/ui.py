# SPDX-License-Identifier: Apache-2.0
"""Terminal UI helpers for human search progress output."""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
import textwrap
from typing import TextIO

STEP_WIDTH = 10
_STEP_COUNTER = 0
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# Cosine-trajectory double helix, width 5, 16 frames per loop.
#
# Two strands oscillate at opposite phase. Position is round(2 + 2·cos φ);
# the partner is its mirror across center, so they always sum to 4 when
# both are visible. Glyph size encodes velocity (|sin φ|): the strand is
# small at the resting peaks, medium mid-arc, and a bold filled circle as
# it accelerates toward the center. At the two per-loop crossings only the
# front strand renders, alternating blue/violet so the eye reads the two
# faces of the helix (front-of-axis vs back-of-axis).
# 12-frame loop. Each side of the rail (LEFT / RIGHT) walks its own
# smooth size wave · → • → ● → • → · → •, with peaks 4 frames apart
# and the two side-waves offset by 6 frames so left and right peak in
# alternation. Single-strand descent never skips a tier.
#
#   LEFT  side sizes by frame:  · • ● • · • ● • · • • •
#   RIGHT side sizes by frame:  ● • · • • • · • ● • · •
#
# Crossings (i=3, i=9) inherit the LEFT side's "mid" so the cross is
# a single mid dot framed by · connectors — visually grounded but
# never abrupt.
HELIX_ROLE_FRAMES = (
    "v d M",   # 0    pos[4,0]   LEFT · violet small · RIGHT ● blue BIG
    "w d m",   # 1    pos[4,0]   LEFT • mid violet · RIGHT • mid blue
    " Wdb ",   # 2    pos[3,1]   LEFT ● BIG violet · RIGHT · small blue
    " dmd ",   # 3    CROSSING   blue mid at centre (front), · flanks
    " bdw ",   # 4    pos[1,3]   LEFT · small blue · RIGHT • mid violet
    "m d w",   # 5    pos[0,4]   LEFT • mid blue · RIGHT • mid violet
    "M d v",   # 6    pos[0,4]   LEFT ● BIG blue · RIGHT · small violet
    "m d w",   # 7    pos[0,4]   LEFT • mid blue · RIGHT • mid violet
    " bdW ",   # 8    pos[1,3]   LEFT · small blue · RIGHT ● BIG violet
    " dwd ",   # 9    CROSSING   violet mid at centre (front), · flanks
    " wdb ",   # 10   pos[3,1]   LEFT • mid violet · RIGHT · small blue
    "w d m",   # 11   pos[4,0]   LEFT • mid violet · RIGHT • mid blue (loop wraps · → ·)
)
# Three visual tiers (● → • → ·) with four logical tiers per strand.
#
# BIG (B/V) and BIG-mid (M/W) share the same glyph ● and differ only in
# ANSI brightness — that keeps the descent ●→●→•→· visually gentle
# (peak isn't disproportionately larger than the next tier down) while
# still letting the cycle ramp through four logical phases.
HELIX_GLYPHS = {
    "B": "●",  # blue strand,    peak (bold)
    "M": "●",  # blue strand,    BIG-mid (regular)
    "m": "•",  # blue strand,    mid
    "b": "·",  # blue strand,    small
    "V": "●",  # violet strand,  peak (bold)
    "W": "●",  # violet strand,  BIG-mid (regular)
    "w": "•",  # violet strand,  mid
    "v": "·",  # violet strand,  small
    "d": "·",  # base-pair connector (neutral white-dim)
    " ": " ",
}
HELIX_FRAMES = tuple(
    "".join(HELIX_GLYPHS[ch] for ch in frame)
    for frame in HELIX_ROLE_FRAMES
)
RAIL_SEPARATOR = "│"
ANSI_RESET = "\x1b[0m"
ANSI_BLUE = "\x1b[1;38;5;39m"
ANSI_BLUE_MID = "\x1b[38;5;39m"
ANSI_BLUE_DIM = "\x1b[2;38;5;39m"
ANSI_CYAN = "\x1b[38;5;81m"
ANSI_VIOLET = "\x1b[1;38;5;177m"
ANSI_VIOLET_MID = "\x1b[38;5;177m"
ANSI_VIOLET_DIM = "\x1b[2;38;5;177m"
ANSI_WHITE_DIM = "\x1b[2;38;5;255m"
ANSI_DIM = "\x1b[2m"

NERD_ICONS = {
    # Font Awesome glyphs are stable across Nerd Font 2.x/3.x and tend to
    # render more consistently than the larger Material Design PUA range.
    "route": "\uf074",      # random
    "scope": "\uf07c",      # folder-open
    "metadata": "\uf02c",   # tags
    "filename": "\uf0f6",   # file-text-o
    "proactive": "\uf140",  # bullseye
    "scan": "\uf002",       # search
    "seed": "\uf06c",       # leaf
    "lazy": "\uf252",       # hourglass-half
    "plan": "\uf0e8",       # sitemap
    "expand": "\uf065",     # expand
    "embed": "\uf12e",      # puzzle-piece
    "cross": "\uf126",      # code-fork
    "budget": "\uf017",     # clock-o
    "busy": "\uf023",       # lock
    "refine": "\uf1de",     # sliders
    "index": "\uf1c0",      # database
    "cascade": "\uf0ec",    # exchange
    "hint": "\uf0eb",       # lightbulb-o
    "setup": "\uf0ad",      # wrench
    "quality": "\uf05d",    # check-circle-o
    "keyword": "\uf031",    # font
    "done": "\uf00c",       # check
    "path": "\uf07c",       # folder-open
    "router": "\uf074",     # random
    "evidence": "\uf05d",   # check-circle-o
    "pool": "\uf0e8",       # sitemap
    "reason": "\uf0eb",     # lightbulb-o
    "recovery": "\uf0ad",   # wrench
}


def nerd_icons_enabled() -> bool:
    """True when Nerd Font glyphs should be shown for human output."""

    raw = (
        os.environ.get("SKYGREP_UI_ICONS")
        or os.environ.get("SKYGREP_NERD_FONT")
        or ""
    ).strip().lower()
    if raw in {"0", "false", "no", "off", "none", "plain"}:
        return False
    if raw in {"1", "true", "yes", "on", "nerd", "nerdfont", "nerd-font"}:
        return True
    if raw in {"helix", "braid"}:
        raw = ""
    if raw:
        return False
    if os.environ.get("CI") or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        return bool(sys.stderr.isatty() or sys.stdout.isatty())
    except Exception:
        return False


def rail_style() -> str:
    """Current rail style.

    The rail is structural output. The visual helix is reserved for the live
    wait animation so normal results remain easy to scan and copy.
    """

    raw = os.environ.get("SKYGREP_UI_RAIL", "").strip().lower()
    if not raw:
        # Backward-compatible with the first experimental screenshots where
        # users tried SKYGREP_UI_ICONS=helix. Icons and rail are separate now,
        # but accepting this keeps the intent understandable.
        raw = os.environ.get("SKYGREP_UI_ICONS", "").strip().lower()
    if raw in {"tree", "plain", "helix", "braid"}:
        return "helix" if raw == "braid" else raw
    if nerd_icons_enabled():
        return "helix"
    if os.environ.get("CI") or os.environ.get("PYTEST_CURRENT_TEST"):
        return "tree"
    try:
        human_tty = sys.stderr.isatty() or sys.stdout.isatty()
    except Exception:
        human_tty = False
    return "helix" if human_tty else "tree"


def animation_style() -> str:
    raw = os.environ.get("SKYGREP_UI_ANIMATION", "").strip().lower()
    if raw in {"0", "false", "no", "off", "none"}:
        return ""
    if raw in {"1", "true", "yes", "on", "helix", "braid", "spin"}:
        return "helix"
    return "helix"


def live_animation_enabled(stream: TextIO | None = None) -> bool:
    """Whether live terminal animation may write control sequences.

    This is intentionally TTY-gated. JSON/agent consumers often merge stderr
    into captured context, so dynamic UI must never appear unless a human is
    using an interactive terminal.
    """

    if not animation_style():
        return False
    force = os.environ.get("SKYGREP_UI_FORCE_ANIMATION", "").strip().lower()
    if force in {"1", "true", "yes", "on"}:
        return True
    stream = stream or sys.stderr
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _label(label: str) -> str:
    clean = (label or "step").strip().replace(" ", "-")[:STEP_WIDTH]
    if nerd_icons_enabled():
        icon = NERD_ICONS.get(clean, NERD_ICONS.get("route", ""))
        return f"{icon} {clean:<{STEP_WIDTH}}"
    return f"{clean:<{STEP_WIDTH}}"


def _step_prefix() -> str:
    style = rail_style()
    if style == "helix":
        return _next_rail_prefix()
    return "├─"


def _detail_prefix() -> str:
    return _next_rail_prefix() if rail_style() == "helix" else "│ "


def _spacer_prefix() -> str:
    return _next_rail_prefix() if rail_style() == "helix" else "│"


def reset_rail_for_tests() -> None:
    global _STEP_COUNTER
    _STEP_COUNTER = 0


def helix_frame(index: int) -> str:
    role_frame = HELIX_ROLE_FRAMES[index % len(HELIX_ROLE_FRAMES)]
    raw = HELIX_FRAMES[index % len(HELIX_FRAMES)]
    if not _colors_enabled():
        return raw
    out: list[str] = []
    for role in role_frame:
        glyph = HELIX_GLYPHS[role]
        if role == "B":
            out.append(_paint(glyph, ANSI_BLUE))
        elif role == "M":
            out.append(_paint(glyph, ANSI_BLUE))  # BIG: bold blue
        elif role == "m":
            out.append(_paint(glyph, ANSI_BLUE_MID))
        elif role == "b":
            out.append(_paint(glyph, ANSI_BLUE_DIM))
        elif role == "V":
            out.append(_paint(glyph, ANSI_VIOLET))
        elif role == "w":
            out.append(_paint(glyph, ANSI_VIOLET_MID))
        elif role == "W":
            out.append(_paint(glyph, ANSI_VIOLET))  # BIG: bold violet
        elif role == "v":
            out.append(_paint(glyph, ANSI_VIOLET_DIM))
        elif role == "d":
            out.append(_paint(glyph, ANSI_WHITE_DIM))
        else:
            out.append(glyph)
    return "".join(out)


def _next_rail_prefix() -> str:
    global _STEP_COUNTER
    prefix = helix_frame(_STEP_COUNTER)
    _STEP_COUNTER += 1
    return prefix


def _rail_separator() -> str:
    return _paint(RAIL_SEPARATOR, ANSI_CYAN)


def _rail_join(prefix: str, content: str, *, continuation_indent: str = "") -> str:
    if rail_style() == "helix":
        return _helix_wrap(prefix, content, continuation_indent=continuation_indent)
    return f"{prefix} {content}"


def _helix_join(prefix: str, content: str) -> str:
    return f"{prefix} {_rail_separator()} {content}"


def _helix_wrap(prefix: str, content: str, *, continuation_indent: str = "") -> str:
    line = _helix_join(prefix, content)
    max_visible_width = max(40, _terminal_columns() - _right_safe_margin())
    if (
        not _ui_wrap_enabled()
        or "\x1b[" in content
        or _visible_width(line) <= max_visible_width
    ):
        return line

    rail_width = _visible_width(_helix_join(prefix, ""))
    content_width = max(20, max_visible_width - rail_width)
    wrapper = textwrap.TextWrapper(
        width=content_width,
        subsequent_indent=continuation_indent,
        break_long_words=True,
        break_on_hyphens=False,
    )
    parts = wrapper.wrap(content) or [""]
    lines = [_helix_join(prefix, parts[0])]
    for part in parts[1:]:
        lines.append(_helix_join(_next_rail_prefix(), part))
    return "\n".join(lines)


def live_line(label: str, message: str, frame_index: int = 0) -> str:
    line = _helix_join(
        helix_frame(frame_index),
        f"{_label(label).strip()} {message}",
    ).rstrip()
    return _truncate_visible(line, _terminal_columns() - 1)


def _colors_enabled() -> bool:
    raw = os.environ.get("SKYGREP_UI_COLOR", "").strip().lower()
    if raw in {"0", "false", "no", "off", "none"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    if os.environ.get("NO_COLOR") or os.environ.get("CI") or os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        return bool(sys.stderr.isatty() or sys.stdout.isatty())
    except Exception:
        return False


def _paint(text: str, color: str) -> str:
    if not _colors_enabled():
        return text
    return f"{color}{text}{ANSI_RESET}"


def _visible_width(text: str) -> int:
    return len(_ANSI_RE.sub("", text))


def _terminal_columns() -> int:
    raw = os.environ.get("SKYGREP_UI_WIDTH", "").strip()
    if raw:
        try:
            return max(40, int(raw))
        except ValueError:
            pass
    try:
        return max(40, shutil.get_terminal_size((100, 24)).columns)
    except Exception:
        return 100


def _right_safe_margin() -> int:
    raw = os.environ.get("SKYGREP_UI_RIGHT_MARGIN", "").strip()
    if raw:
        try:
            return max(0, min(24, int(raw)))
        except ValueError:
            pass
    # Warp and some terminal chrome can report a width that is a few columns
    # wider than the actual drawable cell area. Leave a small guard band so
    # score digits never wrap into the left workflow rail.
    return 8


def rail_prefix_width() -> int:
    """Visible columns consumed by the helix rail plus separator."""

    return _visible_width(_helix_join(HELIX_FRAMES[0], ""))


def available_content_columns(min_width: int = 40) -> int:
    """Terminal columns available to content after the active rail."""

    width = _terminal_columns()
    if rail_style() == "helix":
        width -= rail_prefix_width() + _right_safe_margin()
    return max(min_width, width)


def _ui_wrap_enabled() -> bool:
    raw = os.environ.get("SKYGREP_UI_WRAP", "").strip().lower()
    return raw not in {"0", "false", "no", "off", "none"}


def _truncate_visible(text: str, width: int) -> str:
    if width <= 1 or _visible_width(text) <= width:
        return text
    # Live animation uses this only for mostly-uncolored status text. If
    # color escapes are present, fall back to a conservative plain trim
    # rather than risking a wrapped live line.
    plain = _ANSI_RE.sub("", text)
    return plain[: max(1, width - 1)].rstrip() + "…"


def helix_panel(label: str, message: str, frame_index: int = 0) -> str:
    """Preview a narrow vertical rail sequence for tests/docs."""

    lines = [
        _helix_join(helix_frame(frame_index + i), f"{'':<{len(_label(''))}}")
        for i in range(6)
    ]
    lines.append(live_line(label, message, frame_index + 6))
    return "\n".join(lines)


class LiveHelix:
    """TTY-only narrow left-rail particle animation for foreground waits."""

    def __init__(
        self,
        label: str,
        *,
        stream: TextIO | None = None,
        interval_s: float = 0.08,
    ) -> None:
        self.label = label
        self.stream = stream or sys.stderr
        self.interval_s = max(0.04, interval_s)
        self.enabled = live_animation_enabled(self.stream)
        self._message = ""
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self, message: str = "") -> "LiveHelix":
        if not self.enabled:
            return self
        with self._lock:
            self._message = message
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def update(self, message: str) -> None:
        if not self.enabled:
            print(message, file=self.stream, flush=True)
            return
        with self._lock:
            self._message = message

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.4)
        try:
            self.stream.write("\r\x1b[2K")
            self.stream.flush()
        except Exception:
            pass

    def __enter__(self) -> "LiveHelix":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _run(self) -> None:
        i = 0
        while not self._stop.is_set():
            with self._lock:
                msg = self._message
            try:
                self.stream.write("\r\x1b[2K" + live_line(self.label, msg, i))
                self.stream.flush()
            except Exception:
                return
            i += 1
            self._stop.wait(self.interval_s)


def step(label: str, message: str) -> str:
    """One terminal workflow-rail line for in-progress search stages."""

    label_text = _label(label)
    return _rail_join(
        _step_prefix(),
        f"{label_text} {message}",
        continuation_indent=" " * (len(label_text) + 1),
    )


def detail(message: str) -> str:
    """Continuation line under the current workflow rail."""

    indent = " " * (len(_label("")) + 1)
    return _rail_join(
        _detail_prefix(),
        f"{'':<{len(_label(''))}} {message}",
        continuation_indent=indent,
    )


def spacer() -> str:
    """Blank connector line between workflow sections."""

    if rail_style() == "helix":
        return _rail_join(_spacer_prefix(), "")
    return _spacer_prefix()


def block(text: str) -> str:
    """Attach the active workflow rail to every line of a rendered block."""

    if rail_style() != "helix" or not text:
        return text
    out = []
    for line in text.splitlines():
        continuation_indent = ""
        if line.startswith("│ "):
            continuation_indent = "│ "
        elif line.startswith("│"):
            continuation_indent = "│"
        out.append(
            _rail_join(
                _next_rail_prefix(),
                line,
                continuation_indent=continuation_indent,
            )
        )
    return "\n".join(out)


def done(elapsed: float, quality: str) -> str:
    """Workflow-rail terminator used by every human search footer."""

    if rail_style() == "helix":
        prefix = _next_rail_prefix()
        leader = ""
    else:
        prefix = "╰─"
        leader = "\n"
    if nerd_icons_enabled():
        content = f"{NERD_ICONS['done']} done   {elapsed:.3f}s · quality={quality}"
    else:
        content = f"done   {elapsed:.3f}s · quality={quality}"
    return f"{leader}{_rail_join(prefix, content, continuation_indent=' ' * 7)}"


def rows(rows_: list[tuple[str, str]]) -> str:
    if not rows_:
        return ""
    label_w = max(len(label) for label, _ in rows_)
    if nerd_icons_enabled():
        out = []
        for label, value in rows_:
            icon = NERD_ICONS.get(label, " ")
            out.append(f"   {icon} {label.ljust(label_w)} : {value}")
        rendered = "\n".join(out)
    else:
        rendered = "\n".join(f"   {label.ljust(label_w)} : {value}" for label, value in rows_)
    return block(rendered)
