# local-mgrep 0.12.1 — release notes

A purely visual patch. The CLI now renders results to match the
landing-page hero — cyan repo-relative paths, right-aligned language
pill + bold-green score, dim separator rule, and lightweight syntax
highlighting on the code body. JSON output and pipe / redirect
behaviour are untouched.

## Headline

Before (v0.12.0):

```
=== /Users/.../local-mgrep/local_mgrep/src/config.py:114-128 (score: 0.557) ===
[file: local_mgrep/src/config.py] [lang: python] [symbol: get_config]

def get_config():
    embed_model = os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL)
```

After (v0.12.1):

```
local_mgrep/src/config.py:114-128                                  python  0.557
────────────────────────────────────────────────────────────────────────────────
symbol: get_config

def get_config():
    embed_model = os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL)
```

…with cyan path, dim line range, cyan-pill language, bold-green score,
amber keywords, cyan function names, green strings, and dim grey
comments — close enough to the
<https://danielchen26.github.io/local-mgrep/> hero card to feel
intentional, while staying pure ANSI (no extra dependency).

## What's new

### `local_mgrep/src/render.py` (new)

Owns terminal rendering. Two public functions:

  - `render_terminal_result(r, *, content, max_chars, color, project_root)`
    — full result card with header + separator + symbol line +
    syntax-highlighted body.
  - `render_compact_source(r, *, color)` — one-liner used by the
    `--answer` Sources list.

Color is auto-detected: applied only when stdout is a TTY and
`NO_COLOR` is not set. The standard `NO_COLOR=1` opt-out is
honoured. `MGREP_FORCE_COLOR=1` forces colour on for piped output
(useful for `mgrep ... | less -R`).

### Path display: repo-relative when possible

When the result's path lives under the project root, the CLI now
shows it repo-relative (`local_mgrep/src/config.py`) instead of
absolute (`/Users/.../local-mgrep/local_mgrep/src/config.py`). Same
substring match logic, half the visual noise.

### `[file: ...] [lang: ...] [symbol: ...]` chunk header stripped

Every stored chunk is prefixed with a metadata line useful to the
embedder but redundant in CLI output (the path and language already
appear in the result header). v0.12.1 strips it before display and
surfaces the `symbol:` field on its own dim line where present.

### Lightweight in-house syntax highlighter

Hand-rolled tokeniser covers five token classes — keywords (`def`,
`async`, `if`, ...), function/class names following definition
keywords, type-ish capitalised identifiers, string literals, numbers
& literals (`True`, `False`, `None`), and comments. Works reasonably
on Python / JS / TS / Rust / Go without pulling in `pygments`
(~3 MB).

## Files changed

  - `local_mgrep/src/render.py` (new) — ~210 lines.
  - `local_mgrep/src/cli.py` — every result-render site (5) now
    delegates to `render_terminal_result` or `render_compact_source`.
  - `pyproject.toml`: 0.12.0 → 0.12.1.
  - `docs/local-mgrep-0.12.1.md` (this file).
  - `docs/index.html`: version label v0.12.0 → v0.12.1.
  - `docs/assets/og-image.svg/png`, `hero-dark.svg`: version stamp.
  - `docs/README.md`, `README.md`: 0.12.1 entry / bullet.

## Compatibility

  - **55 / 55 unit tests pass** — no test touches the render module.
  - All flags / env / per-project DB layout from prior releases
    unchanged.
  - `--json` output: byte-for-byte 0.12.0.
  - `mgrep ... | less` / `mgrep ... > file` / `mgrep ... | jq`:
    auto-detected as non-TTY, falls back to plain text. No ANSI
    escapes ever land in pipes by default.
  - The retrieval pipeline (cascade, lexical shortcut) is unchanged.

## What did not change

  - `lexical_shortcut()` and the four-condition gate from v0.12.0.
  - Cascade retrieval pipeline.
  - All flags / env vars from prior releases.
  - JSON output schema.

## Install

```
pip install --upgrade local-mgrep
```

To preview:

```
mgrep "your query"            # auto-coloured in your terminal
NO_COLOR=1 mgrep "your query" # opt-out, plain text
mgrep "your query" --json     # machine output, unchanged from v0.12.0
```
