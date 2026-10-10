# SPDX-License-Identifier: Apache-2.0
"""Render the "What it does" animation (light + dark GIFs): one terminal vignette per capability.

    python docs/assets/anim/capabilities.py      # writes capabilities_{light,dark}.gif next to this file

The queries and outputs are the README's illustrative examples, shown in the CLI's own output formats; the footer
says so. Needs matplotlib, pillow, ffmpeg and a CJK font (Noto Sans CJK) for the multilingual example.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402
from PIL import Image  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from agent_compare import THEMES, ease, mix, pt, seg  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
W, H, DPI, SCALE = 1280, 640, 100, 1.5
FPS, SLOT = 20, 3.4
SANS = ["TeX Gyre Heros", "Liberation Sans", "DejaVu Sans", "Noto Sans CJK SC", "Noto Sans CJK JP"]
MONO = ["DejaVu Sans Mono", "Noto Sans CJK SC", "Noto Sans CJK JP"]

# (name, one-line description, command, output lines, caption). Output styles: path, code, dim, ok, warn, json;
# ("cmd", text) starts a follow-up command.
CAPS = [
    ("Finds code by meaning", "not just keywords",
     ['skygrep "where is session refresh?"'],
     [("path", "═══ auth/middleware.py:78-94          score 0.91 · python"),
      ("code", "async def renew_session(req: Request):"),
      ("code", "    # swap the access cookie when the refresh JWT is still valid"),
      ("dim", "╰─ done   0.5s · quality=BEST")],
     "No line says “session refresh”: the hit is matched by meaning."),
    ("Code, PDFs, Word, notes", "one query, one ranked list",
     ['skygrep "the design doc on the rate limiter rewrite"'],
     [("path", "→ docs/rate-limiter-redesign.md          [markdown]"),
      ("path", "→ designs/q3-rewrite.pdf                  [pdf]"),
      ("path", "→ notes/limiter-review.docx              [docx]"),
      ("dim", "╰─ done   0.4s · quality=BEST")],
     "Markdown, PDF, Word and plain text are indexed the same way."),
    ("Any language", "100+ languages, mixed welcome",
     ['skygrep "我昨天写的 cascade 调度代码"'],
     [("path", "→ src/storage.py:847  ·  cascade_search()"),
      ("cmd", 'skygrep "where is the cascade scheduler?"'),
      ("path", "→ src/storage.py:847  ·  cascade_search()"),
      ("dim", "  same hit, either language")],
     "Ask in English, Chinese or both; bge-m3 embeddings bridge the languages."),
    ("100% offline", "no cloud, no telemetry",
     ["skygrep doctor"],
     [("ok", "Ollama runtime    ✓ http://localhost:11434"),
      ("ok", "Embed model       ✓ bge-m3"),
      ("ok", "Project index     ✓ 54 files / 406 chunks")],
     "Indexing, search and ranking all run on your machine via Ollama."),
    ("Built for AI agents", "MCP + token-lean output",
     ['skygrep --agent-slim "where are page numbers validated?"'],
     [("json", '{"results":[{"path":"pkg/paginator.py","start_line":18,…}],'),
      ("json", ' "outline":[{"lines":["18: def validate_number(self, number):"]}],'),
      ("json", ' "next":"skygrep symbols pkg/views.py -q …"}'),
      ("ok", "MCP tools: search · agent_context · symbols")],
     "One-step setup for Claude Code, Codex and Cursor: skygrep setup."),
    ("Verifiable", "explains every hit",
     ['skygrep -x "find pyproject.toml in this repo"'],
     [("dim", "├─ route   router: filename · conf=0.95"),
      ("path", "╭─ pyproject.toml ───────────────── [toml]  1.000"),
      ("dim", "│ via: filename-lookup · token \"pyproject.toml\""),
      ("cmd", 'skygrep --strict "where is authorization enforced?"'),
      ("warn", "strict_verification: inconclusive  ·  exit 2")],
     "-x shows why each result matched; --strict refuses to vouch for weak evidence."),
    ("Works from any folder", "finds the right project",
     ['cd /tmp/scratch && skygrep "where does the parallel umbrella dispatch?"'],
     [("dim", "├─ proactive umbrella · sibling roots"),
      ("dim", "│             cascade still searching"),
      ("path", "→ ~/code/skylakegrep/src/cli.py:912  ·  cascade ‖ proactive umbrella"),
      ("dim", "╰─ done   1.1s · quality=BEST")],
     "Ask from the wrong directory; a bounded search still reaches the project."),
]
DURATION = SLOT * len(CAPS)


class Frame:
    def __init__(self, theme):
        self.tk = THEMES[theme]
        plt.rcParams.update({"font.family": "sans-serif", "font.sans-serif": SANS, "font.monospace": MONO})
        self.fig = plt.figure(figsize=(W / DPI, H / DPI), dpi=DPI * SCALE, facecolor=self.tk["bg"])

    def begin(self):
        self.fig.clf()
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.ax.set_xlim(0, W)
        self.ax.set_ylim(H, 0)
        self.ax.axis("off")

    def text(self, x, y, s, size=16, color=None, weight="normal", ha="left", alpha=1.0, mono=False, z=5):
        if alpha <= 0 or not s:
            return None
        return self.ax.text(x, y, s, fontsize=pt(size), color=color or self.tk["ink"], fontweight=weight, ha=ha,
                            va="baseline", alpha=alpha, family=MONO if mono else SANS, zorder=z)

    def box(self, x, y, w, h, r=10, fc=None, ec="none", lw=1.0, alpha=1.0, z=1):
        if alpha <= 0 or w <= 0:
            return
        self.ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={min(r, h / 2, w / 2)}",
                                         fc=fc or self.tk["panel"], ec=ec, lw=lw, alpha=alpha, zorder=z,
                                         mutation_aspect=1))

    def image(self):
        self.fig.canvas.draw()
        return Image.fromarray(np.asarray(self.fig.canvas.buffer_rgba())[..., :3].copy())


def draw(f, t):
    tk = f.tk
    f.begin()
    idx = min(int(t // SLOT), len(CAPS) - 1)
    lt = t - idx * SLOT                       # local time inside the vignette
    accent = tk["slim"]

    f.text(40, 44, "SKYLAKEGREP  ·  WHAT IT DOES", size=16.5, color=tk["muted"], weight="bold")
    f.text(40, 84, "Ask in plain language. Get the right file and line.", size=34, weight="bold")
    f.text(40, 112, "Code, PDFs and notes  ·  any language  ·  100% local  ·  built for AI agents", size=17.5,
           color=tk["ink2"])

    # ------------------------------------------------------------------ capability list
    lx, ly, rh = 40, 140, 62
    for i, (name, desc, *_rest) in enumerate(CAPS):
        y = ly + i * rh
        active = i == idx
        if active:
            f.box(lx, y, 360, rh - 4, r=10, fc=tk["panel"], ec=tk["edge"], z=1)
            f.box(lx, y + 8, 4, rh - 20, r=2, fc=accent, z=2)
            f.box(lx + 58, y + rh - 11, 284 * seg(lt, 0, SLOT), 2.5, r=1.25, fc=mix(accent, tk["panel"], 0.2), z=2)
        done = i < idx
        badge_fc = accent if (active or done) else tk["track"]
        f.box(lx + 18, y + 13, 26, 26, r=13, fc=badge_fc, z=3)
        f.text(lx + 31, y + 31, str(i + 1), size=13.5, weight="bold", ha="center",
               color=tk["bg"] if (active or done) else tk["muted"], z=4)
        f.text(lx + 58, y + 27, name, size=17, weight="bold", color=tk["ink"] if active else tk["ink2"], z=4)
        f.text(lx + 58, y + 46, desc, size=13.5, color=tk["muted"], z=4)

    # ------------------------------------------------------------------ terminal
    tx, ty, tw, th = 430, 140, 810, 434
    f.box(tx, ty, tw, th, r=14, fc=tk["panel"], ec=tk["edge"], z=1)
    for k, c in enumerate(("#ff5f57", "#febc2e", "#28c840")):
        f.box(tx + 20 + k * 20, ty + 16, 12, 12, r=6, fc=c, z=2)
    f.text(tx + tw / 2, ty + 27, "skygrep  ·  runs locally", size=13.5, color=tk["muted"], ha="center")

    name, desc, cmds, lines, caption = CAPS[idx]
    a_in = ease(seg(lt, 0.0, 0.25)) * (1 - ease(seg(lt, SLOT - 0.2, SLOT)))
    x0, y = tx + 26, ty + 74
    f.text(x0, y, "$", size=15, mono=True, color=accent, weight="bold", alpha=a_in)
    cmd = cmds[0]
    n = int(len(cmd) * seg(lt, 0.15, 1.05))
    typed = cmd[:n]
    f.text(x0 + 20, y, typed + ("▌" if n < len(cmd) else ""), size=15, mono=True, alpha=a_in)
    y += 34
    for j, (style, line) in enumerate(lines):
        t_on = 1.15 + 0.12 * j
        a = ease(seg(lt, t_on, t_on + 0.2)) * a_in
        if style == "cmd":
            f.text(x0, y + 10, "$", size=15, mono=True, color=accent, weight="bold", alpha=a)
            f.text(x0 + 20, y + 10, line, size=15, mono=True, alpha=a)
            y += 44
            continue
        color = {"path": accent, "code": tk["ink"], "dim": tk["muted"], "ok": tk["ok_fg"], "warn": tk["bad_fg"],
                 "json": tk["ink2"]}[style]
        f.text(x0, y, line, size=14.5, mono=True, color=color, weight="bold" if style == "path" else "normal",
               alpha=a)
        y += 28
    a_cap = ease(seg(lt, 1.7, 2.0)) * a_in
    f.box(tx + 18, ty + th - 66, tw - 36, 46, r=10, fc=mix(accent, tk["panel"], 0.82), alpha=a_cap, z=2)
    f.text(tx + 36, ty + th - 37, caption, size=16, color=tk["ink"], alpha=a_cap, z=3)

    f.text(40, 618, "Illustrative queries in the CLI's real output formats  ·  pip install skylakegrep", size=13,
           color=tk["muted"])
    f.text(1240, 618, "Apache-2.0 · fully local", size=13, color=tk["muted"], ha="right")


def render(theme, out):
    f = Frame(theme)
    tmp = tempfile.mkdtemp(prefix=f"capabilities_{theme}_")
    n = int(round(DURATION * FPS))
    for i in range(n):
        draw(f, i / FPS)
        f.image().save(os.path.join(tmp, f"f_{i:05d}.png"))
    pat, pal = os.path.join(tmp, "f_%05d.png"), os.path.join(tmp, "palette.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS), "-i", pat, "-vf",
                    "palettegen=max_colors=256:stats_mode=full", pal], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS), "-i", pat, "-i", pal, "-lavfi",
                    "paletteuse=dither=sierra2_4a:diff_mode=rectangle", "-loop", "0", out], check=True)
    shutil.rmtree(tmp)
    plt.close(f.fig)


def main(argv):
    if argv and argv[0] == "--still":
        for k in (int(a) for a in argv[1:] or ["0"]):
            for theme in ("dark", "light"):
                f = Frame(theme)
                draw(f, k * SLOT + SLOT - 0.4)
                f.image().save(os.path.join(HERE, f"capabilities_{theme}_{k}.png"))
        return
    for theme in ("light", "dark"):
        out = os.path.join(HERE, f"capabilities_{theme}.gif")
        render(theme, out)
        print(out, os.path.getsize(out))


if __name__ == "__main__":
    main(sys.argv[1:])
