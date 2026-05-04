# local-mgrep 0.13.0 — release notes

A two-track polish release. **Smart routing now also handles
filename-lookup queries** (a third tier above the v0.12.0 lexical
content shortcut and the cascade), and **every result is rendered
inside a proper rounded card frame** with Pygments-driven syntax
highlighting tuned to the website hero.

## Headline

```
$ mgrep "where is eb1b file?" -m 5

╭─ EB1B_Denial_Analysis.pdf ─────────────────────────────  pdf   1.000
│ size:   108.2 KB    modified: 2025-12-08 14:13    type: pdf
╰─────────────────────────────────────────────────────────────────────

╭─ Tianchi Chen EB-1B filing.pdf ────────────────────────  pdf   1.000
│ size:    91.4 KB    modified: 2025-04-12 09:21    type: pdf
╰─────────────────────────────────────────────────────────────────────

[0.602s · filename-lookup · 5 match(es)]
```

```
$ mgrep "config defaults loaded from environment" -m 2

╭─ local_mgrep/src/config.py:114-128 ───────────────────  python   0.557
│ symbol: get_config
│
│ def get_config():
│     embed_model = os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL)
│     return {
│         "ollama_url": os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
│         ...
╰─────────────────────────────────────────────────────────────────────────

[0.110s · cascade=cheap (gap=0.0202 τ=0.0150) ...]
```

(Real terminals show cyan paths, amber keywords, bright cyan
function names, green strings, dim grey comments — the full
website-hero palette via Pygments ``monokai``.)

## What's new

### Filename-lookup shortcut (`auto_index.filename_shortcut`)

A third routing tier, sitting **above** the v0.12.0 lexical content
shortcut. Queries like ``where is eb1b file?`` /
``find package.json`` / ``show me README`` are now routed directly
to ``find -iname '*token*'`` and answered in ~10 ms — no index
needed (so it works even in directories that have never been
indexed, like ``~/Downloads``).

Conservative four-condition gate (same philosophy as v0.12.0):

  1. Query lowercased contains an explicit lookup-intent phrase
     (``find / where is / locate / show me / open ...``) **or** the
     standalone word ``file`` / ``files``.
  2. After stripping stop-words, at least one ``name-like`` token
     remains (length 3-40, alphanumeric with optional ``._-``).
  3. ``find -iname '*<token>*'`` returns >= 1 and <= 30 actual
     files (not dirs, not dotfiles, not under
     ``node_modules / .venv / __pycache__``).
  4. The longest matching path's basename literally contains the
     token (case-insensitive). Guards against fluke substring
     matches deep in the tree.

Borderline queries fall through to the lexical content shortcut
and then to cascade — semantic recall is never sacrificed for
filename-routing speed.

### Routing decision tree (now three-tier)

  1. **L-2: Filename lookup** (new in v0.13.0) — ~10 ms — answers
     ``where is X file?`` without touching the index.
  2. **L-1: Lexical content shortcut** (v0.12.0) — ~50 ms —
     answers ripgrep-friendly content questions without touching
     the embedder.
  3. **L0+: Confidence-gated semantic cascade** — sub-second to
     ~3 s — answers vocabulary-mismatch questions.

Every tier is conservative; queries never get hijacked. The CLI
banner under each result tells you which tier ran (``filename-
lookup`` / ``rg-shortcut`` / ``cascade=cheap`` / ``cascade=
escalated``).

### `--filename-shortcut / --no-filename-shortcut` flag

Default: **on**. Pass ``--no-filename-shortcut`` to bypass the
filename tier (useful for benchmarking or for forcing semantic
search on a query that happens to contain "file"). Honoured
in agentic / answer modes the same way as the v0.12.0 lexical
shortcut.

### Result card rendering — proper frames + Pygments

Every result is now wrapped in a **rounded card frame** with
top + bottom rules and a left-side bar:

```
╭─ <path:lines> ──────────────────────────  <pill>   <score>
│ <symbol if present>
│
│ <syntax-highlighted body>
╰────────────────────────────────────────────────────────────
```

The right border is intentionally omitted so we never have to
compute pad-to-width around ANSI escape sequences (which would be
brittle once Pygments injects styled tokens).

Code body is now syntax-highlighted with **Pygments**
(``Terminal256Formatter``, ``monokai`` style by default — override
with ``MGREP_PYGMENTS_STYLE=<name>``). All 300+ Pygments lexers are
available; the indexer's ``language`` field is fed straight into
``get_lexer_by_name``. JSON snippets get pretty-printed when
valid; log snippets keep their timestamps dim. Filename-lookup
results show a metadata pill row (``size · modified · type``).

### `pygments` is now a hard dependency

Added to ``[project.dependencies]`` (~3 MB). The render module
falls back to a hand-rolled highlighter if Pygments is missing,
so the install is never broken — but PyPI installs will pull it
in automatically.

## Files changed

  - `local_mgrep/src/auto_index.py` — `filename_shortcut()`
    (~120 lines, plus tuning constants).
  - `local_mgrep/src/cli.py` — `--filename-shortcut/--no-filename-shortcut`
    flag, call site placed BEFORE the index-ready check so the
    filename tier works on un-indexed directories.
  - `local_mgrep/src/render.py` — full rewrite: card frames,
    Pygments formatter, JSON/log/markdown content-type dispatch,
    fallback hand-rolled highlighter.
  - `tests/test_filename_shortcut.py` (new) — 9 tests covering
    happy paths + every condition's negative branch + shape
    validation + dir-vs-file exclusion.
  - `pyproject.toml` — `pygments>=2.0` dep added; version
    0.12.1 → 0.13.0.
  - `docs/local-mgrep-0.13.0.md` (this file).
  - `docs/index.html`, `docs/assets/{og-image.svg,og-image.png,
    hero-dark.svg}` — version stamps.
  - `docs/README.md`, `README.md` — index entry / release bullet.

## Compatibility

  - **64 / 64 unit tests pass** (47 prior + 8 lexical shortcut +
    9 filename shortcut).
  - All 0.4.x – 0.12.1 flags / env / per-project DB layout
    unchanged.
  - Existing project indexes are picked up as-is.
  - `--json` output: byte-for-byte 0.12.1 — content-type rendering
    only affects the human-facing terminal path.
  - Pipe / redirect (`mgrep ... | cat`, `mgrep ... > out.txt`):
    auto-detected as non-TTY, falls back to plain ASCII frames
    without colour. JSON pretty-print still applies (helps
    readability when piping into `less`).
  - The retrieval pipeline (cascade + lexical shortcut from
    v0.12.0) is unchanged.

## What did not change

  - Cascade retrieval pipeline.
  - Lexical content shortcut and its four-condition gate.
  - All flags / env vars from prior releases.
  - JSON output schema.
  - Default models.

## Honest accuracy gates

  - **Unit tests** — 64 / 64 pass.
  - **Lexical shortcut tests** (v0.12.0) — 8 / 8 still pass.
  - **Filename shortcut tests** (new) — 9 / 9 pass, every
    condition both branches.
  - **Self-test 30-task semantic benchmark** — held at 30 / 30
    @ top-k 10 (the same number v0.12.0 hit; the filename
    shortcut sits ABOVE the cascade and so doesn't intercept any
    of the 30 content questions, all of which lack lookup-intent
    phrases).
  - **CLI smoke test (filename path)**: `mgrep "where is eb1b file?"`
    in `~/Downloads/` returns the 5 EB1B documents in 0.6 s.
    Same query under v0.12.1 returned poetry.lock / SVG / events.jsonl
    junk — a real bug fix.
  - **CLI smoke test (cascade path)**: `mgrep "config defaults loaded
    from environment" --no-rg-shortcut --no-filename-shortcut`
    returns the same chunks as v0.12.1 with the new card framing.

## Install

```
pip install --upgrade local-mgrep
```

To preview:

```
mgrep "where is README"           # filename-lookup tier (~10 ms)
mgrep "auth login"                # lexical content tier (~50 ms)
mgrep "how does the cascade work" # semantic cascade tier
mgrep "..." --no-filename-shortcut --no-rg-shortcut # force cascade
NO_COLOR=1 mgrep "..."            # opt out of ANSI colour
```
