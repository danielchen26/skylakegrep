# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from skylakegrep.src import ui
from skylakegrep.src.render import render_terminal_result


class _TtyBuffer:
    def isatty(self):
        return True


class _PlainBuffer:
    def isatty(self):
        return False


class TerminalUiTests(unittest.TestCase):
    def test_plain_rail_is_default(self):
        with patch.dict(os.environ, {}, clear=True):
            ui.reset_rail_for_tests()
            self.assertEqual(
                ui.step("route", "router: semantic"),
                "├─ route      router: semantic",
            )
            self.assertEqual(
                ui.done(0.42, "BEST"),
                "\n╰─ done   0.420s · quality=BEST",
            )
            self.assertEqual(ui.spacer(), "│")

    def test_nerd_font_mode_uses_helix_and_step_icons(self):
        with patch.dict(os.environ, {"SKYGREP_UI_ICONS": "nerd"}, clear=True):
            ui.reset_rail_for_tests()
            first = ui.step("route", "router: semantic")
            second = ui.step("filename", "matches:")
            self.assertTrue(first.startswith(f"{ui.HELIX_FRAMES[0]} │ "), first)
            self.assertTrue(second.startswith(f"{ui.HELIX_FRAMES[1]} │ "), second)
            self.assertIn("route", first)
            self.assertIn("router: semantic", first)
            self.assertIn("filename", second)
            self.assertEqual(ui.spacer(), f"{ui.HELIX_FRAMES[2]} │ ")
            self.assertIn("path", ui.rows([("path", "filename-lookup")]))
            self.assertIn("done", ui.done(0.42, "BEST"))

    def test_helix_can_be_requested_without_icons(self):
        with patch.dict(os.environ, {"SKYGREP_UI_RAIL": "helix"}, clear=True):
            ui.reset_rail_for_tests()
            line = ui.step("route", "router: semantic")
            self.assertTrue(line.startswith(f"{ui.HELIX_FRAMES[0]} │ route"), line)
            self.assertNotIn("╭╮", line)
            self.assertNotIn("╰╯", line)
            self.assertNotIn("", line)

    def test_nerd_font_icons_default_on_for_tty(self):
        with patch.dict(os.environ, {"SKYGREP_UI_COLOR": "off"}, clear=True), patch.object(
            ui.sys.stderr, "isatty", return_value=True
        ), patch.object(ui.sys.stdout, "isatty", return_value=False):
            ui.reset_rail_for_tests()
            line = ui.step("route", "router: semantic")
        self.assertIn(ui.NERD_ICONS["route"], line)
        self.assertIn("route", line)

    def test_legacy_icons_helix_value_still_gets_tty_icons(self):
        with patch.dict(os.environ, {"SKYGREP_UI_ICONS": "helix", "SKYGREP_UI_COLOR": "off"}, clear=True), patch.object(
            ui.sys.stderr, "isatty", return_value=True
        ), patch.object(ui.sys.stdout, "isatty", return_value=False):
            ui.reset_rail_for_tests()
            line = ui.step("route", "router: semantic")
        self.assertTrue(line.startswith(f"{ui.HELIX_FRAMES[0]} │ "), line)
        self.assertIn(ui.NERD_ICONS["route"], line)

    def test_live_helix_preview_is_narrow_left_rail(self):
        with patch.dict(os.environ, {"SKYGREP_UI_COLOR": "off"}, clear=True):
            first = ui.helix_panel("semantic", "embedding", 0)
            second = ui.helix_panel("semantic", "embedding", 1)
        self.assertEqual(len(first.splitlines()), 7)
        self.assertNotEqual(first, second)
        self.assertIn("semantic", first)
        self.assertIn("embedding", first)
        rail_lines = first.splitlines()[:-1]
        frame_tokens = {frame.strip() for frame in ui.HELIX_FRAMES}
        self.assertTrue(all(line.split("│", 1)[0].strip() in frame_tokens for line in rail_lines))
        self.assertTrue(all("│" in line for line in first.splitlines()))
        self.assertTrue(any(ch in first for ch in ("●", "•", "·")))

    def test_block_continues_rail_through_result_lines(self):
        with patch.dict(os.environ, {"SKYGREP_UI_RAIL": "helix", "SKYGREP_UI_COLOR": "off"}, clear=True):
            ui.reset_rail_for_tests()
            ui.step("route", "router: semantic")
            rendered = ui.block("card line 1\ncard line 2")
            footer = ui.rows([("path", "cosine"), ("router", "fast-intent")])
        self.assertIn(f"{ui.HELIX_FRAMES[1]} │ card line 1", rendered)
        self.assertIn(f"{ui.HELIX_FRAMES[2]} │ card line 2", rendered)
        self.assertIn(f"{ui.HELIX_FRAMES[3]} │    path", footer)
        self.assertIn(f"{ui.HELIX_FRAMES[4]} │    router", footer)

    def test_helix_wraps_long_status_lines_under_current_terminal_width(self):
        long_message = (
            "cross exploring 5 candidate roots... batch embed failed for 3 chunks: "
            "HTTPConnectionPool(host='localhost', port=11434): Read timed out"
        )
        with patch.dict(
            os.environ,
            {
                "SKYGREP_UI_RAIL": "helix",
                "SKYGREP_UI_COLOR": "off",
                "SKYGREP_UI_WIDTH": "58",
            },
            clear=True,
        ):
            ui.reset_rail_for_tests()
            rendered = ui.step("semantic", long_message)
        lines = rendered.splitlines()
        self.assertGreater(len(lines), 1)
        self.assertTrue(all("│" in line for line in lines))
        self.assertTrue(all(len(line) <= 58 for line in lines), rendered)
        self.assertTrue(all(line[:3].strip() for line in lines), rendered)
        self.assertIn("HTTPConnectionP", rendered)
        self.assertIn("Pool(ho", rendered)
        self.assertIn("st='localhost'", rendered)

    def test_helix_done_does_not_insert_unrailed_blank_line(self):
        with patch.dict(os.environ, {"SKYGREP_UI_RAIL": "helix", "SKYGREP_UI_COLOR": "off"}, clear=True):
            ui.reset_rail_for_tests()
            done = ui.done(0.42, "BEST")
        self.assertFalse(done.startswith("\n"), done)
        self.assertTrue(done.startswith(f"{ui.HELIX_FRAMES[0]} │ "), done)

    def test_helix_frames_form_smooth_double_helix(self):
        """The frame strip is a cosine-trajectory double helix.

        Asserts smoothness invariants only — exact frame contents are
        allowed to evolve as the visual is tuned.
        """
        rendered = "".join(ui.HELIX_FRAMES)
        self.assertNotIn("╲", rendered)
        self.assertNotIn("╱", rendered)
        # Three visual tiers must appear (peak and BIG-mid share ●;
        # ANSI bold/regular distinguishes them in colored output).
        self.assertIn("●", rendered)
        self.assertIn("•", rendered)
        self.assertIn("·", rendered)
        self.assertEqual(ui.HELIX_GLYPHS["B"], "●")
        self.assertEqual(ui.HELIX_GLYPHS["V"], "●")
        self.assertEqual(ui.HELIX_GLYPHS["M"], "●")
        self.assertEqual(ui.HELIX_GLYPHS["W"], "●")
        self.assertEqual(ui.HELIX_GLYPHS["m"], "•")
        self.assertEqual(ui.HELIX_GLYPHS["w"], "•")
        self.assertEqual(ui.HELIX_GLYPHS["b"], "·")
        self.assertEqual(ui.HELIX_GLYPHS["v"], "·")
        self.assertEqual(ui.HELIX_GLYPHS["d"], "·")
        # Every frame uses the rail width.
        self.assertEqual({len(frame) for frame in ui.HELIX_FRAMES}, {5})
        # Every depth tier is exercised on both strands. Peak letters
        # (B/V) are reserved for cross-frame overrides; the smooth
        # cycle uses the BIG/mid/small triplet on each strand.
        all_roles = "".join(ui.HELIX_ROLE_FRAMES)
        for tier in "MmbWwv":
            self.assertIn(tier, all_roles, f"missing depth tier {tier!r}")
        # Strand positions: None at crossings (only one strand visible).
        fg = [
            next((i for i, role in enumerate(frame) if role in "BMmb"), None)
            for frame in ui.HELIX_ROLE_FRAMES
        ]
        comp = [
            next((i for i, role in enumerate(frame) if role in "VWwv"), None)
            for frame in ui.HELIX_ROLE_FRAMES
        ]
        # All five columns are visited by at least one strand.
        visited = {p for p in fg + comp if p is not None}
        self.assertEqual(visited, {0, 1, 2, 3, 4})
        # When both strands are visible they sit at opposite phase.
        for i, (b, v) in enumerate(zip(fg, comp)):
            if b is not None and v is not None:
                self.assertEqual(b + v, 4, f"strands not symmetric at frame {i}")
        # Position changes are smooth (≤ 1 column per tick) when both
        # strands are visible across the transition.
        wrap_fg = fg + fg[:1]
        wrap_comp = comp + comp[:1]
        for prev, current in zip(wrap_fg, wrap_fg[1:]):
            if prev is not None and current is not None:
                self.assertLessEqual(abs(current - prev), 1)
        for prev, current in zip(wrap_comp, wrap_comp[1:]):
            if prev is not None and current is not None:
                self.assertLessEqual(abs(current - prev), 1)
        # Cosine dwells naturally at peaks; allow up to 3 ticks at the same
        # column on each strand (the geometric lower bound at width 5,
        # 16 frames).
        def longest_run(seq):
            best = 1
            run = 1
            for prev, current in zip(seq, seq[1:]):
                run = run + 1 if current == prev and current is not None else 1
                best = max(best, run)
            return best
        self.assertLessEqual(longest_run(fg), 3)
        self.assertLessEqual(longest_run(comp), 3)
        # Both strands must be visible on most frames; only the small set of
        # crossings hide one strand by design.
        n_frames = len(ui.HELIX_ROLE_FRAMES)
        self.assertGreaterEqual(sum(1 for b in fg if b is not None), n_frames - 2)
        self.assertGreaterEqual(sum(1 for v in comp if v is not None), n_frames - 2)
        # Connector dot is present whenever the strands are separated.
        for i, (b, v) in enumerate(zip(fg, comp)):
            if b is not None and v is not None:
                self.assertIn("d", ui.HELIX_ROLE_FRAMES[i], f"frame {i}")
        # Loop closes smoothly (last frame transitions to first).
        if fg[-1] is not None and fg[0] is not None:
            self.assertLessEqual(abs(fg[-1] - fg[0]), 1)
        if comp[-1] is not None and comp[0] is not None:
            self.assertLessEqual(abs(comp[-1] - comp[0]), 1)

    def test_helix_secondary_chain_uses_violet_when_colored(self):
        with patch.dict(os.environ, {"SKYGREP_UI_COLOR": "on"}, clear=True):
            frames = "".join(ui.helix_frame(i) for i in range(len(ui.HELIX_FRAMES)))
        self.assertIn("38;5;39", frames)
        self.assertIn("2;38;5;39", frames)
        self.assertIn("38;5;177", frames)
        self.assertIn("2;38;5;177", frames)
        self.assertIn("\x1b[1;", frames)
        self.assertIn("2;38;5;255", frames)

    def test_helix_result_cards_keep_frame_inside_content_lane(self):
        result = {
            "path": "/repo/src/session.ts",
            "language": "ts",
            "score": 0.637,
            "snippet": "size: 1 KB",
            "fallback": "filename-lookup",
        }
        with patch.dict(os.environ, {"SKYGREP_UI_RAIL": "helix", "SKYGREP_UI_COLOR": "off"}, clear=True):
            ui.reset_rail_for_tests()
            rendered = render_terminal_result(
                result,
                content=True,
                color=False,
                project_root="/repo",
            )
        self.assertIn("│ ╭─ src/session.ts", rendered)
        self.assertIn("│ │ size: 1 KB", rendered)
        self.assertIn("│ ╰", rendered)
        self.assertIn("src/session.ts", rendered)
        self.assertIn("size: 1 KB", rendered)

    def test_helix_result_header_reserves_space_for_rail(self):
        result = {
            "path": "/repo/very/long/path/with/many/segments/security/rate-limiter.ts",
            "language": "typescript",
            "score": 0.534,
            "snippet": "const limiter = createLimiter();",
        }
        with patch.dict(
            os.environ,
            {
                "SKYGREP_UI_RAIL": "helix",
                "SKYGREP_UI_COLOR": "off",
                "SKYGREP_UI_WIDTH": "72",
            },
            clear=True,
        ):
            ui.reset_rail_for_tests()
            rendered = render_terminal_result(
                result,
                content=True,
                color=False,
                project_root="/repo",
            )
        for line in rendered.splitlines():
            self.assertLessEqual(len(line), 72, rendered)
        header = next(line for line in rendered.splitlines() if "0.534" in line)
        self.assertLessEqual(len(header), 72, rendered)
        self.assertIn("0.534", rendered)
        self.assertNotIn("\n4", rendered)
        self.assertIn("very/long/path/with/many/segments/security", rendered)
        self.assertIn("-limiter.ts", rendered)
        self.assertIn("rat", rendered)
        self.assertIn("imiter.ts", rendered)

    def test_helix_colored_body_lines_wrap_before_terminal_can_wrap(self):
        result = {
            "path": "/repo/docs/plan.md",
            "language": "markdown",
            "score": 0.592,
            "snippet": (
                "| `scripts/` | Paper-side figure, measurement, and summary scripts. | "
                "`notes/` | Stable scientific content should be folded into the "
                "package before submission. |"
            ),
        }
        with patch.dict(
            os.environ,
            {
                "SKYGREP_UI_RAIL": "helix",
                "SKYGREP_UI_COLOR": "on",
                "SKYGREP_UI_WIDTH": "84",
            },
            clear=True,
        ):
            ui.reset_rail_for_tests()
            rendered = render_terminal_result(
                result,
                content=True,
                color=True,
                project_root="/repo",
            )
        self.assertTrue(all("│" in line for line in rendered.splitlines()), rendered)
        for line in rendered.splitlines():
            self.assertLessEqual(ui._visible_width(line), 84, rendered)

    def test_helix_content_results_stay_framed_and_out_of_gutter(self):
        result = {
            "path": "/repo/paper/docs/plans/2026-04-14-long-generic-revision-plan.md",
            "language": "markdown",
            "score": 0.592,
            "start_line": 1,
            "end_line": 40,
            "snippet": (
                "# Generic Revision Plan\n\n"
                "| `scripts/` | Figure, measurement, and summary scripts. | "
                "`notes/` | Stable scientific content should be folded into "
                "the package before submission. | `archive/` | Historical "
                "drafts retained for reference. |\n\n"
                "**Goal:** This deliberately long paragraph should wrap inside "
                "the card body without sending score digits or body continuations "
                "into the workflow rail."
            ),
        }
        with patch.dict(
            os.environ,
            {
                "SKYGREP_UI_RAIL": "helix",
                "SKYGREP_UI_COLOR": "off",
                "SKYGREP_UI_WIDTH": "96",
            },
            clear=True,
        ):
            ui.reset_rail_for_tests()
            rendered = render_terminal_result(
                result,
                content=True,
                color=False,
                project_root="/repo",
                detail="standard",
            )
        lines = rendered.splitlines()
        self.assertTrue(any("│ ╭─ " in line and "score=0.592" in line for line in lines), rendered)
        self.assertTrue(any("│ │ # Generic Revision Plan" in line for line in lines), rendered)
        self.assertTrue(any("│ ╰" in line for line in lines), rendered)
        self.assertFalse(any(line.strip() == "score=0.592" for line in lines), rendered)
        for line in lines:
            self.assertLessEqual(ui._visible_width(line), 96, rendered)
            if line.strip():
                self.assertIn("│", line, rendered)

    def test_particle_colors_are_blue_violet_and_subtle_neutral_not_yellow(self):
        with patch.dict(os.environ, {"SKYGREP_UI_COLOR": "on"}, clear=True):
            frames = "".join(ui.helix_frame(i) for i in range(len(ui.HELIX_FRAMES)))
        self.assertIn("38;5;39", frames)
        self.assertIn("38;5;177", frames)
        self.assertIn("2;38;5;255", frames)
        self.assertNotIn("38;5;220", frames)
        self.assertNotIn("38;5;214", frames)

    def test_live_animation_is_opt_in_and_tty_gated(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(ui.live_animation_enabled(_TtyBuffer()))
            self.assertFalse(ui.live_animation_enabled(_PlainBuffer()))

        with patch.dict(os.environ, {"SKYGREP_UI_ANIMATION": "helix"}, clear=True):
            self.assertTrue(ui.live_animation_enabled(_TtyBuffer()))
            self.assertFalse(ui.live_animation_enabled(_PlainBuffer()))
            self.assertNotEqual(ui.helix_frame(0), ui.helix_frame(1))
            self.assertIn("semantic", ui.live_line("semantic", "embedding", 0))
            self.assertIn("embedding", ui.live_line("semantic", "embedding", 0))

        with patch.dict(os.environ, {"SKYGREP_UI_WIDTH": "48"}, clear=True):
            live = ui.live_line("semantic", "x" * 120, 0)
        self.assertEqual(len(live.splitlines()), 1)
        self.assertLessEqual(len(live), 47)

        with patch.dict(os.environ, {"SKYGREP_UI_ANIMATION": "off"}, clear=True):
            self.assertFalse(ui.live_animation_enabled(_TtyBuffer()))


if __name__ == "__main__":
    unittest.main()
