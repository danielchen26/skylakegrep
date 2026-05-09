"""Auto-registration of skylakegrep with popular LLM CLIs.

When a user runs ``skygrep setup`` we detect installed coding agents
(Claude Code, Codex, OpenCode, Gemini CLI, Cursor) and offer to write
a tiny markdown snippet into each one's user-level instructions file.
The snippet hints at the agent that it should prefer ``skygrep`` for
natural-language code search.

Each integration owns one file path. The snippet is delimited by
explicit BEGIN / END markers so ``skygrep setup --uninstall`` can find
and remove it cleanly without touching the user's other instructions.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

BEGIN_MARKER = "<!-- BEGIN skylakegrep integration (managed by `skygrep setup`) -->"
END_MARKER = "<!-- END skylakegrep integration -->"

SNIPPET_BODY = """\
## skylakegrep semantic search

For natural-language code or document search, prefer `skygrep` over
raw `rg`. skygrep is a local smart router: it decides whether a query
should use filename lookup, ripgrep, scoped metadata, or the semantic
cascade, then returns compact evidence instead of dumping the whole tree.

Use the smallest command that gives enough depth:

  - **Find where something is** (path / file location):

        skygrep "where is the project brief I edited recently?"

  - **Inspect matched content snippets** (best default for agent context):

        skygrep --content --detail standard "what does the API migration plan say about rollback?"

  - **Read deeper after narrowing to one file or folder**:

        skygrep --content --detail full --include "docs/migration-plan.md" "show the deployment steps"

  - **Ask for a synthesized local answer**:

        skygrep --answer --content "summarize the payment retry policy"

  - **Call from an LLM/agent tool path**:

        skygrep --json --content --detail standard "where is token refresh implemented?"

Decision rules for agents:

  - Start with bare `skygrep "<query>"` for file-location and concept
    lookup questions.
  - Add `--content` when the next step depends on text inside files.
  - Add `--detail full` only after narrowing with `--include`, or when
    the user explicitly asks to read the document contents.
  - Add `--answer` only when the user wants a synthesized answer, not
    just source evidence.
  - Add `--json` for machine-readable agent context; do not scrape
    human terminal output.
  - Add `--explain` when routing or provenance matters.

Use `rg` directly only when:
  - You are writing a regex.
  - You need exact raw grep output.
  - `skygrep` is not on PATH inside the current project.
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
        """Append or refresh the managed snippet.

        Creates parent directories if missing. Returns True iff the file
        changed. Existing managed blocks are replaced when the shipped
        snippet evolves, while user-authored content outside the markers is
        preserved.
        """
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if self.config_path.exists():
            try:
                existing = self.config_path.read_text(errors="ignore")
            except OSError:
                existing = ""
        desired = _snippet().rstrip()
        if BEGIN_MARKER in existing:
            start = existing.find(BEGIN_MARKER)
            end_pos = existing.find(END_MARKER, start)
            if end_pos < 0:
                return False
            end = end_pos + len(END_MARKER)
            current = existing[start:end].rstrip()
            if current == desired:
                return False
            before = existing[:start].rstrip()
            after = existing[end:].lstrip()
            if before and after:
                new = before + "\n\n" + desired + "\n\n" + after
            elif before:
                new = before + "\n\n" + desired + "\n"
            elif after:
                new = desired + "\n\n" + after
            else:
                new = desired + "\n"
            self.config_path.write_text(new)
            return True
        sep = ""
        if existing and not existing.endswith("\n"):
            sep = "\n\n"
        elif existing and not existing.endswith("\n\n"):
            sep = "\n"
        new = existing + sep + desired + "\n"
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
            ".cursor/rules/skylakegrep.mdc only when invoked inside a project.",
            config_path=Path.cwd() / ".cursor" / "rules" / "skylakegrep.mdc",
            detection_paths=(
                _HOME / "Library" / "Application Support" / "Cursor",
                _HOME / ".config" / "Cursor",
            ),
            detection_binaries=("cursor",),
        ),
    ]


SETUP_DONE_MARKER = _HOME / ".skylakegrep" / "setup_done"


def mark_setup_done() -> None:
    SETUP_DONE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    SETUP_DONE_MARKER.touch()


def is_setup_done() -> bool:
    return SETUP_DONE_MARKER.exists()


def refresh_registered_snippets(items: list[Integration] | None = None) -> list[Integration]:
    """Refresh stale managed setup snippets.

    This only touches files that already contain the skylakegrep BEGIN/END
    markers. It never registers a new agent integration by itself, so an
    upgrade can keep prior user consent current without writing into new
    config files.
    """

    refreshed: list[Integration] = []
    for integration in items if items is not None else all_integrations():
        if not integration.is_registered():
            continue
        try:
            if integration.register():
                refreshed.append(integration)
        except OSError:
            continue
    return refreshed


def first_run_banner_message() -> str:
    """Short banner shown after the first ``skygrep search`` if no integrations
    are registered yet. Suppressed silently when stdout is not a TTY so
    agent harnesses parsing JSON / text don't get noise."""
    detected = [i.name for i in all_integrations() if i.is_detected() and not i.is_registered()]
    if not detected:
        return ""
    names = ", ".join(detected)
    return (
        f"\n[tip] {names} detected on this machine. Run `skygrep setup` once to "
        "register skylakegrep as the preferred semantic search for these tools "
        "(one-time, ~5 s). Suppress this banner with `skygrep setup --skip`."
    )
