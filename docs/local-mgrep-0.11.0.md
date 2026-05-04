# local-mgrep 0.11.0 — release notes

A user-experience release. After `pip install`, run `mgrep setup` once
to register local-mgrep as the preferred semantic search for **every
mainstream LLM CLI** detected on your machine. The agent will then use
mgrep instead of `rg` for natural-language code questions.

## Headline

```
$ mgrep setup
Detected the following LLM CLIs on your machine:

  ✓ Claude Code    → ~/.claude/CLAUDE.md
  ✓ Codex          → ~/.codex/AGENTS.md
  ✓ OpenCode       → ~/.config/opencode/AGENTS.md
  ✓ Gemini CLI     → ~/.gemini/GEMINI.md
  ✓ Cursor         → ./.cursor/rules/local-mgrep.mdc

Register local-mgrep with Claude Code? [Y/n] y
  ✓ wrote snippet to ~/.claude/CLAUDE.md
…

Done. Registered 5 integration(s).
```

The snippet hints to the agent that it should prefer
`mgrep "<query>"` over `rg` for natural-language code questions and
fall back to `rg` automatically when mgrep isn't on PATH for the
current project.

## What's new

### `mgrep setup` interactive command

Detects every supported LLM CLI on the user's machine (config dir
existence and/or binary on `PATH`) and writes a small markdown
snippet into each one's user-level instructions file. The snippet
lives between explicit BEGIN / END markers so a future
`mgrep setup --uninstall` can find and remove it cleanly without
touching the user's other instructions.

Flags:
  - `mgrep setup` — interactive, prompts before each integration.
  - `mgrep setup --yes` / `-y` — register all detected without
    prompting.
  - `mgrep setup --list` — show what's detected and registered;
    no writes.
  - `mgrep setup --uninstall` — remove every snippet `mgrep setup`
    has previously written.
  - `mgrep setup --skip` — mark setup as done without registering
    anything (suppresses the first-run banner).

### Supported LLM CLIs

| Tool | Detection | Config file we write |
| --- | --- | --- |
| Claude Code | `~/.claude/` exists OR `which claude` | `~/.claude/CLAUDE.md` |
| Codex (OpenAI) | `~/.codex/` exists OR `which codex` | `~/.codex/AGENTS.md` |
| OpenCode | `~/.config/opencode/` exists OR `which opencode` | `~/.config/opencode/AGENTS.md` |
| Gemini CLI | `~/.gemini/` exists OR `which gemini` | `~/.gemini/GEMINI.md` |
| Cursor | `~/Library/Application Support/Cursor/` (mac) or `~/.config/Cursor/` (linux) OR `which cursor` | `./.cursor/rules/local-mgrep.mdc` (project-level) |

For Cursor we write a project-level rules file because Cursor's
user-level rules live in app settings UI rather than a flat config
file. Run `mgrep setup` from inside the project root to land
this file.

### First-run banner

When a user runs `mgrep "<query>"` for the first time after install
and at least one supported LLM CLI is detected but not yet
registered, a one-line tip appears under the search results:

```
[tip] Claude Code, Codex detected on this machine. Run `mgrep setup`
      once to register local-mgrep as the preferred semantic search
      for these tools (one-time, ~5 s). Suppress this banner with
      `mgrep setup --skip`.
```

The banner is suppressed under `--json` (machine consumers parsing
output), in non-TTY contexts (agents piping output), and after
`mgrep setup` has run at least once.

### `mgrep doctor` reports registration

The doctor health-check now lists every detected LLM CLI and whether
it's currently registered:

```
mgrep doctor
  …
  LLM CLI: Claude Code      ✓ registered
  LLM CLI: Codex            ✓ registered
  LLM CLI: OpenCode         ✓ registered
  LLM CLI: Gemini CLI       ✓ registered
  LLM CLI: Cursor           ✓ registered
```

## Files changed

  - `local_mgrep/src/integrations.py` (new) — `Integration` dataclass
    with `register` / `unregister` / `is_detected` / `is_registered`
    methods; `all_integrations()` factory; setup-done marker
    management; first-run banner builder.
  - `local_mgrep/src/cli.py` — new `setup` subcommand (with
    `--yes` / `--list` / `--uninstall` / `--skip`); first-run banner
    after the search status line; doctor lists registered CLIs.
  - `tests/test_integrations.py` (new) — 7 tests covering registry
    semantics, idempotence, and CLI list/skip.
  - `docs/local-mgrep-0.11.0.md` (this file).
  - `docs/assets/hero-dark.svg`, `og-image.svg/png` — version bump
    v0.10.0 → v0.11.0.
  - `docs/index.html` — brand version label v0.10.0 → v0.11.0.
  - `pyproject.toml` — version bump.
  - `README.md` — Releases bullet adds 0.11.0 + Quickstart mentions
    `mgrep setup` as part of one-time onboarding.

## Compatibility

  - **47 / 47 unit tests pass** (40 original + 7 new integration
    tests).
  - All 0.4.x – 0.10.0 flags / env / per-project DB layout
    unchanged.
  - Existing project indexes are picked up as-is.
  - The retrieval pipeline is byte-for-byte 0.10.0.
  - Snippets are delimited by explicit markers and `mgrep setup
    --uninstall` removes them cleanly without touching surrounding
    user-authored content.

## What did not change

  - Retrieval pipeline byte-for-byte 0.10.0.
  - All flags / env vars from prior releases remain valid.
  - Default models: `OLLAMA_EMBED_MODEL=nomic-embed-text`,
    `OLLAMA_LLM_MODEL=qwen2.5:3b`,
    `OLLAMA_HYDE_MODEL=qwen2.5:3b`,
    `OLLAMA_KEEP_ALIVE=-1`.

## Install

```
pip install --upgrade local-mgrep
mgrep setup        # one-time: register with detected LLM CLIs
```
