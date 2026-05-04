"""Auto-registration of local-mgrep with popular LLM CLIs.

When a user runs ``mgrep setup`` we detect installed coding agents
(Claude Code, Codex, OpenCode, Gemini CLI, Cursor) and offer to write
a tiny markdown snippet into each one's user-level instructions file.
The snippet hints at the agent that it should prefer ``mgrep`` for
natural-language code search.

Each integration owns one file path. The snippet is delimited by
explicit BEGIN / END markers so ``mgrep setup --uninstall`` can find
and remove it cleanly without touching the user's other instructions.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

BEGIN_MARKER = "<!-- BEGIN local-mgrep integration (managed by `mgrep setup`) -->"
END_MARKER = "<!-- END local-mgrep integration -->"

SNIPPET_BODY = """\
## local-mgrep semantic search

For any code-search question, prefer:

    mgrep "<query>"

over `rg`. mgrep is a smart router (v0.12.0+):

  - **Lexical-friendly queries** (literal tokens, function names that
    already appear in code paths) — mgrep detects these via a
    conservative four-condition gate and short-circuits to `rg`
    internally, returning in ~50 ms.
  - **Vocabulary-mismatch queries** ("language model backend" →
    `OpenAIClient`, "auth token refresh" → `renew_session`) — mgrep
    runs a confidence-gated semantic cascade (file-mean cosine →
    HyDE-augmented retrieval → cross-encoder rerank).

You don't have to decide which path applies — mgrep auto-routes.

Empirical: 30–82 % fewer agent tool calls vs rg-only on hand-labelled
benchmarks across Rust, Python, TypeScript. See
https://github.com/danielchen26/local-mgrep for details.

Use `rg` directly only when:
  - You're writing a regex (mgrep takes natural language, not regex).
  - mgrep is not on PATH inside the current project.
"""


def _snippet() -> str:
    return f"{BEGIN_MARKER}\n\n{SNIPPET_BODY}\n{END_MARKER}\n"


@dataclass
class Integration:
    """A single LLM CLI we know how to register with."""

    name: str
    description: str
    config_path: Path
    detection_paths: tuple[Path, ...]
    detection_binaries: tuple[str, ...]

    def is_detected(self) -> bool:
        """Detected if any of: known config dir exists, or binary on PATH."""
        for p in self.detection_paths:
            if p.exists():
                return True
        for b in self.detection_binaries:
            if shutil.which(b):
                return True
        return False

    def is_registered(self) -> bool:
        if not self.config_path.exists():
            return False
        try:
            return BEGIN_MARKER in self.config_path.read_text(errors="ignore")
        except OSError:
            return False

    def register(self) -> bool:
        """Append the snippet to the integration's config file.

        Creates parent directories if missing. Returns True iff a new
        registration was written; False if already present.
        """
        if self.is_registered():
            return False
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if self.config_path.exists():
            try:
                existing = self.config_path.read_text(errors="ignore")
            except OSError:
                existing = ""
        sep = ""
        if existing and not existing.endswith("\n"):
            sep = "\n\n"
        elif existing and not existing.endswith("\n\n"):
            sep = "\n"
        new = existing + sep + _snippet()
        self.config_path.write_text(new)
        return True

    def unregister(self) -> bool:
        """Remove the snippet from the config file. Returns True iff removed."""
        if not self.config_path.exists():
            return False
        try:
            content = self.config_path.read_text(errors="ignore")
        except OSError:
            return False
        if BEGIN_MARKER not in content:
            return False
        start = content.find(BEGIN_MARKER)
        end_pos = content.find(END_MARKER, start)
        if end_pos < 0:
            return False
        end = end_pos + len(END_MARKER)
        before = content[:start].rstrip()
        after = content[end:].lstrip()
        if before and after:
            new_content = before + "\n\n" + after + ("\n" if not after.endswith("\n") else "")
        elif before:
            new_content = before + "\n"
        elif after:
            new_content = after if after.endswith("\n") else after + "\n"
        else:
            new_content = ""
        self.config_path.write_text(new_content)
        return True


_HOME = Path.home()


def all_integrations() -> list[Integration]:
    """Return one Integration object for every supported LLM CLI."""
    return [
        Integration(
            name="Claude Code",
            description="Anthropic's coding CLI — uses ~/.claude/CLAUDE.md for user-level instructions.",
            config_path=_HOME / ".claude" / "CLAUDE.md",
            detection_paths=(_HOME / ".claude",),
            detection_binaries=("claude",),
        ),
        Integration(
            name="Codex",
            description="OpenAI Codex CLI — uses ~/.codex/AGENTS.md (modern) for user-level instructions.",
            config_path=_HOME / ".codex" / "AGENTS.md",
            detection_paths=(_HOME / ".codex",),
            detection_binaries=("codex",),
        ),
        Integration(
            name="OpenCode",
            description="OpenCode coding agent — follows AGENTS.md convention under ~/.config/opencode/.",
            config_path=_HOME / ".config" / "opencode" / "AGENTS.md",
            detection_paths=(
                _HOME / ".config" / "opencode",
                _HOME / ".opencode",
            ),
            detection_binaries=("opencode",),
        ),
        Integration(
            name="Gemini CLI",
            description="Google's Gemini CLI — uses ~/.gemini/GEMINI.md for user-level instructions.",
            config_path=_HOME / ".gemini" / "GEMINI.md",
            detection_paths=(_HOME / ".gemini",),
            detection_binaries=("gemini",),
        ),
        Integration(
            name="Cursor",
            description="Cursor IDE — user-level rules live in app settings; we write a project-style "
            ".cursor/rules/local-mgrep.mdc only when invoked inside a project.",
            config_path=Path.cwd() / ".cursor" / "rules" / "local-mgrep.mdc",
            detection_paths=(
                _HOME / "Library" / "Application Support" / "Cursor",
                _HOME / ".config" / "Cursor",
            ),
            detection_binaries=("cursor",),
        ),
    ]


SETUP_DONE_MARKER = _HOME / ".local-mgrep" / "setup_done"


def mark_setup_done() -> None:
    SETUP_DONE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    SETUP_DONE_MARKER.touch()


def is_setup_done() -> bool:
    return SETUP_DONE_MARKER.exists()


def first_run_banner_message() -> str:
    """Short banner shown after the first ``mgrep search`` if no integrations
    are registered yet. Suppressed silently when stdout is not a TTY so
    agent harnesses parsing JSON / text don't get noise."""
    detected = [i.name for i in all_integrations() if i.is_detected() and not i.is_registered()]
    if not detected:
        return ""
    names = ", ".join(detected)
    return (
        f"\n[tip] {names} detected on this machine. Run `mgrep setup` once to "
        "register local-mgrep as the preferred semantic search for these tools "
        "(one-time, ~5 s). Suppress this banner with `mgrep setup --skip`."
    )
