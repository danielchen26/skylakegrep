# SPDX-License-Identifier: Apache-2.0
"""Render the README comparison animation (light + dark GIFs) from agent_compare.json.

    python docs/assets/anim/agent_compare.py            # writes agent_compare_{light,dark}.gif next to this file

Needs matplotlib, pillow and ffmpeg. Every number comes from agent_compare.json, which records where it was
measured; edit the JSON (e.g. with a new receipt) and re-run instead of editing the GIFs.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import to_hex, to_rgb  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402
from PIL import Image  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
W, H, DPI, SCALE = 1280, 720, 100, 1.5
FPS, DURATION, HOLD = 20, 15.0, 3.0
SANS = ["TeX Gyre Heros", "Liberation Sans", "DejaVu Sans"]
MONO = ["DejaVu Sans Mono", "Liberation Mono"]

THEMES = {
    "light": dict(bg="#ffffff", panel="#f6f8fa", edge="#d1d9e0", ink="#1f2328", ink2="#59636e", muted="#818b98",
                  grid="#e8ebef", track="#e6eaef", slim="#2a78d6", rg="#eb6834", first="#8c959f",
                  ok_fg="#1a7f37", ok_bg="#dafbe1", bad_fg="#c4321c", bad_bg="#ffebe4", hl="#ddf4ff"),
    "dark": dict(bg="#0d1117", panel="#151b23", edge="#30363d", ink="#f0f6fc", ink2="#9198a1", muted="#7d8590",
                 grid="#21262d", track="#262c36", slim="#3987e5", rg="#d95926", first="#6e7681",
                 ok_fg="#3fb950", ok_bg="#16301f", bad_fg="#f0683f", bad_bg="#3b1d14", hl="#0c2d4a"),
}


def ease(x):
    x = float(np.clip(x, 0.0, 1.0))
    return 4 * x ** 3 if x < 0.5 else 1 - (-2 * x + 2) ** 3 / 2


def seg(t, t0, t1):
    return float(np.clip((t - t0) / max(t1 - t0, 1e-9), 0.0, 1.0))


def mix(c1, c2, a):
    return to_hex((1 - a) * np.array(to_rgb(c1)) + a * np.array(to_rgb(c2)))


def pt(px):
    return px * 72.0 / DPI


def ktok(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    return f"{n / 1000:.1f}k"


class Frame:
    """A pixel-coordinate canvas (origin top-left) that is cleared and redrawn every frame."""

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

    def text(self, x, y, s, size=16, color=None, weight="normal", ha="left", va="baseline", alpha=1.0,
             mono=False, z=5):
        if alpha <= 0:
            return None
        return self.ax.text(x, y, s, fontsize=pt(size), color=color or self.tk["ink"], fontweight=weight, ha=ha,
                            va=va, alpha=alpha, family="monospace" if mono else "sans-serif", zorder=z)

    def box(self, x, y, w, h, r=10, fc=None, ec="none", lw=1.0, alpha=1.0, z=1):
        if alpha <= 0 or w <= 0:
            return None
        p = FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={min(r, h / 2, w / 2)}",
                           fc=fc or self.tk["panel"], ec=ec, lw=lw, alpha=alpha, zorder=z, mutation_aspect=1)
        self.ax.add_patch(p)
        return p

    def chip(self, x_right, y, s, fg, bg, size=15, alpha=1.0, z=8):
        """A rounded label right-aligned at x_right."""
        if alpha <= 0:
            return
        t = self.text(x_right - 12, y + 14, s, size=size, color=fg, weight="bold", ha="right",
                      va="center_baseline", alpha=alpha, z=z + 1)
        self.fig.canvas.draw()
        bb = t.get_window_extent(renderer=self.fig.canvas.get_renderer())
        wd = bb.width / SCALE + 24
        self.box(x_right - wd, y, wd, 28, r=14, fc=bg, alpha=alpha, z=z)

    def width(self, t):
        if t is None:
            return 0.0
        self.fig.canvas.draw()
        return t.get_window_extent(renderer=self.fig.canvas.get_renderer()).width / SCALE

    def image(self):
        self.fig.canvas.draw()
        buf = np.asarray(self.fig.canvas.buffer_rgba())
        return Image.fromarray(buf[..., :3].copy())


STEP_T0, STEP_DT = 1.1, 0.85      # step i of either panel appears at STEP_T0 + i * STEP_DT


def draw(f, d, t):
    tk, ex = f.tk, d["example"]
    f.begin()
    a_head = ease(seg(t, 0.0, 0.6))

    # ------------------------------------------------------------------ header
    f.text(40, 44, "SKYLAKEGREP 0.8.0  ·  TOKEN-LEAN AGENT LOOP", size=16.5, color=tk["muted"], weight="bold",
           alpha=a_head)
    f.text(40, 84, "Same tasks. The right file. A fraction of the tokens.", size=34, weight="bold", alpha=a_head)
    f.text(40, 112, "A task counts only when the agent's context holds the right file and every literal evidence term"
           "  ·  60 pinned tasks in 6 public repos", size=17.5, color=tk["ink2"], alpha=a_head)

    # ------------------------------------------------------------------ example task line
    a_task = ease(seg(t, 0.4, 1.0))
    f.box(40, 128, 1200, 30, r=8, fc=tk["panel"], alpha=a_task, z=1)
    lab = f.text(54, 148, f"Example task · {ex['repo']}", size=15, color=tk["ink"], weight="bold", alpha=a_task)
    f.text(54 + max(f.width(lab), 150) + 12, 148, f"“{ex['question']}”", size=15, color=tk["ink2"], alpha=a_task)

    # ------------------------------------------------------------------ two agent panels
    panels = [
        (40, "rg", "rg agent", "grep · rank · outline · read   (250-line grep, 2,000-line read caps)", ex["rg-agent"]),
        (650, "slim", "skygrep --agent-slim", "compact anchors + declaration outline, then skygrep symbols",
         ex["skygrep-slim"]),
    ]
    py, ph, pw = 168, 300, 590
    tok_max = max(ex["rg-agent"]["tokens"], ex["skygrep-slim"]["tokens"]) * 1.04
    for x, key, name, desc, run in panels:
        a_p = ease(seg(t, 0.5, 1.1))
        f.box(x, py, pw, ph, r=14, fc=tk["panel"], ec=tk["edge"], alpha=a_p, z=1)
        f.box(x + 20, py + 22, 14, 14, r=7, fc=tk[key], alpha=a_p, z=3)
        f.text(x + 44, py + 36, name, size=23, weight="bold", alpha=a_p, mono=(key == "slim"))
        f.text(x + 44, py + 60, desc, size=15, color=tk["ink2"], alpha=a_p)

        steps = run["steps"]
        cum = 0.0
        for i, (label, tok) in enumerate(steps):
            t_on = STEP_T0 + i * STEP_DT
            a_s = ease(seg(t, t_on, t_on + 0.35))
            yy = py + 88 + i * 20
            f.text(x + 20, yy, label, size=13, mono=True, color=tk["ink"], alpha=a_s)
            f.text(x + pw - 20, yy, f"+{ktok(tok)}", size=13, mono=True, color=tk["ink2"], ha="right", alpha=a_s)
            cum += tok * ease(seg(t, t_on, t_on + 0.6))

        done_t = STEP_T0 + len(steps) * STEP_DT
        if key == "slim":
            a_f = ease(seg(t, done_t, done_t + 0.5))
            fy = py + 88 + len(steps) * 20 - 2
            f.box(x + 16, fy, pw - 32, 78, r=8, fc=tk["hl"], ec=mix(tk["slim"], tk["panel"], 0.4), alpha=a_f, z=2)
            path, *lines = run["found"]
            f.text(x + 30, fy + 24, path, size=13.5, mono=True, weight="bold", color=tk["ink"], alpha=a_f, z=4)
            for j, ln in enumerate(lines):
                f.text(x + 30, fy + 46 + j * 20, ln, size=12.5, mono=True, color=tk["ink"], alpha=a_f, z=4)
            f.text(x + 20, fy + 104, "stops as soon as the right file and both anchors are in context",
                   size=14, color=tk["ink2"], alpha=a_f)

        # running context meter
        my = py + ph - 26
        f.text(x + 20, my - 8, "context read by the agent", size=13.5, color=tk["muted"], alpha=a_p)
        f.box(x + 20, my, pw - 40, 12, r=6, fc=tk["track"], alpha=a_p, z=2)
        f.box(x + 20, my, (pw - 40) * cum / tok_max, 12, r=6, fc=tk[key], alpha=a_p, z=3)
        f.text(x + pw - 20, my - 8, f"{cum / 1000:.1f}k tokens", size=13.5, color=tk["ink"], weight="bold",
               ha="right", alpha=a_p)

        a_v = ease(seg(t, done_t + 0.15, done_t + 0.6))
        if run["solved"]:
            f.chip(x + pw - 18, py + 18, f"found · {run['calls']} calls · {ktok(run['tokens'])} tokens",
                   tk["ok_fg"], tk["ok_bg"], alpha=a_v)
        else:
            f.chip(x + pw - 18, py + 18, f"missed the answer file · {run['calls']} calls · {ktok(run['tokens'])} tokens",
                   tk["bad_fg"], tk["bad_bg"], alpha=a_v)

    # ------------------------------------------------------------------ all-task bars
    pol = d["policies"]
    series = [("skygrep-slim", "slim"), ("rg-agent", "rg"), ("skygrep-first", "first")]
    t_bars = STEP_T0 + len(ex["rg-agent"]["steps"]) * STEP_DT + 0.5
    a_leg = ease(seg(t, t_bars - 0.4, t_bars + 0.2))
    lx = 40
    f.text(lx, 500, "All 60 tasks:", size=15.5, weight="bold", alpha=a_leg)
    lx += 118
    for k, ck in series:
        f.box(lx, 488, 14, 14, r=3, fc=tk[ck], alpha=a_leg, z=3)
        tt = f.text(lx + 22, 500, pol[k]["label"], size=15.5, color=tk["ink2"], alpha=a_leg)
        if tt is not None:
            f.fig.canvas.draw()
            lx += tt.get_window_extent(renderer=f.fig.canvas.get_renderer()).width / SCALE + 50
        else:
            lx += 260

    charts = [
        ("tasks solved", "higher is better", "solved", lambda v: f"{int(v)}/60", 60),
        ("median tokens per task", "lower is better", "median_tokens", lambda v: ktok(v), None),
        ("total tokens, all tasks", "lower is better", "total_tokens", lambda v: ktok(v), None),
    ]
    g = ease(seg(t, t_bars, t_bars + 2.0))
    cw, cx0 = 380, 40
    for ci, (title, note, field, fmt, vmax) in enumerate(charts):
        cx = cx0 + ci * (cw + 30)
        f.text(cx, 534, title, size=16, weight="bold", alpha=a_leg)
        f.text(cx + cw, 534, note, size=13.5, color=tk["muted"], ha="right", alpha=a_leg)
        top = vmax or max(pol[k][field] for k, _ in series)
        for ri, (k, ck) in enumerate(series):
            by = 546 + ri * 34
            v = pol[k][field]
            f.box(cx, by, cw - 92, 24, r=5, fc=tk["track"], alpha=a_leg, z=2)
            f.box(cx, by, (cw - 92) * (v / top) * g, 24, r=5, fc=tk[ck], alpha=a_leg, z=3)
            f.text(cx + cw, by + 18, fmt(v * g if g < 1 else v), size=15, weight="bold", ha="right",
                   alpha=a_leg)

    # ------------------------------------------------------------------ footer
    prov = d["provenance"]
    f.text(40, 680, f"Scripted agent harness · {prov['trials']} trial · tokens = {prov['tokens']} · stand-in "
           "embeddings (semantic lane off) — bge-m3 receipt pending", size=13, color=tk["muted"], alpha=a_head)
    f.text(40, 704, "data: docs/assets/anim/agent_compare.json  ·  reproduce: benchmarks/compare_policies.py",
           size=13, color=tk["muted"], alpha=a_head)
    f.text(1240, 704, "Apache-2.0 · fully local", size=13, color=tk["muted"], ha="right", alpha=a_head)


def render(theme, data, out):
    f = Frame(theme)
    tmp = tempfile.mkdtemp(prefix=f"agent_compare_{theme}_")
    n = int(round(DURATION * FPS))
    for i in range(n):
        draw(f, data, i / FPS)
        f.image().save(os.path.join(tmp, f"f_{i:05d}.png"))
    for j in range(int(round(HOLD * FPS))):
        shutil.copyfile(os.path.join(tmp, f"f_{n - 1:05d}.png"), os.path.join(tmp, f"f_{n + j:05d}.png"))
    pat, pal = os.path.join(tmp, "f_%05d.png"), os.path.join(tmp, "palette.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS), "-i", pat, "-vf",
                    "palettegen=max_colors=256:stats_mode=full", pal], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(FPS), "-i", pat, "-i", pal, "-lavfi",
                    "paletteuse=dither=sierra2_4a:diff_mode=rectangle", "-loop", "0", out], check=True)
    shutil.rmtree(tmp)
    plt.close(f.fig)


def main(argv):
    data = json.load(open(os.path.join(HERE, "agent_compare.json")))
    if argv and argv[0] == "--still":
        for theme in ("light", "dark"):
            f = Frame(theme)
            draw(f, data, DURATION)
            f.image().save(os.path.join(HERE, f"agent_compare_{theme}_still.png"))
        return
    for theme in ("light", "dark"):
        out = os.path.join(HERE, f"agent_compare_{theme}.gif")
        render(theme, data, out)
        print(out, os.path.getsize(out))


if __name__ == "__main__":
    main(sys.argv[1:])
