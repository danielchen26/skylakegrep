# skylakegrep 0.2.4 — release notes

`0.2.4` makes the CLI proactively help the user instead of silently
shrugging. Four new intelligent-assistance behaviours, all
non-blocking, all silenced by `SKYGREP_NO_HINTS=1` for users / CI
that want quiet output.

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact <chentianchi@gmail.com>.

## What changed

### 1. Out-of-scope query detection

Queries like `"我最近工作上的十个文件"` (recently-modified files)
or `"list the largest 5 files"` are *metadata* queries — they want
filesystem mtime / size sort, not content search. skygrep can't
answer those well; the user wants `git log --name-only` or
`find -mtime`. `0.2.4` detects the pattern up front and prints
the hint with the right command, then still runs the search so
the user isn't blocked:

```
$ skygrep "我最近工作上的十个文件"
💡 Heads up: "我最近工作上的十个文件" looks like a metadata query
   (contains '最近' (recency-by-mtime)). skygrep is a *content*
   search tool; the answer you probably want is:
       git log --name-only --pretty=format: HEAD~30..HEAD | sort -u | head -10
       or: find . -type f -mtime -7 -not -path '*/.*'
       or: git diff --name-only HEAD~10..HEAD
   Running semantic search anyway — set SKYGREP_NO_HINTS=1 to suppress.
```

The detection is conservative: a query has to contain a metadata
token (recency / size / listing) AND no semantic-intent token
(`how`, `where`, `what`, `function`, `class`, `怎么`, `如何`,
`函数`, …) AND be ≤ 12 words. This rules out false flags on
queries like *"where is the recent change to auth flow"* (mentions
`recent` but is asking about a specific behaviour).

### 2. Typo correction for unknown flags

When the user types `skygrep search --tup 10`, click's default
error is "Error: No such option: --tup" with no suggestion.
`0.2.4` catches `click.NoSuchOption`, runs `difflib.get_close_matches`
against every long-form flag registered on every command, and
suggests the closest match:

```
$ skygrep search "auth" --tup 10
Unknown flag '--tup'. Did you mean '--top'?  Run `skygrep search --help` for the full list.
```

The cutoff is 0.6 (`difflib`'s default) — catches one-or-two-character
edits without firing on completely-different strings (`--xyz` →
no suggestion, falls back to click's default).

Subcommand typos are not currently corrected: the bare-form router
already routes unknown first-args to `search`, so `skygrep serach
"auth"` is treated as a query rather than a typoed subcommand.
That's intentional — bare-form magic is the most common usage.

### 3. Low-confidence result hints

After every search, the cascade telemetry tells us the top-1
cosine score and the σ-gap. When both are below the noise floor
(top-1 < 0.30, σ-gap < 0.005), the result is shaky and the user
should know about a recovery path:

```
[0.93s · path=cosine-cheap · ... · σ-gap=0.0030 < τ=0.0050 (adaptive) → escalated to rerank · quality=BEST]
⚠ Top-1 score is low (cosine=0.18) and the cascade σ-gap is below the noise floor.
  Possible recoveries:
       skygrep "<query>" --agentic       # decompose into subqueries
       skygrep "<query>" --top 30        # widen the window
       skygrep "<more specific tokens>"  # rephrase with code identifiers
```

When the search returns zero results entirely, the hint instead
points at `skygrep doctor` / `skygrep stats` so the user can
diagnose whether the index is missing the file type they're after.

The floors are tunable via `SKYGREP_LOW_CONF_TOP1_FLOOR` and
`SKYGREP_LOW_CONF_SIGMA_FLOOR`.

### 4. First-run nudge

The first time a user runs `skygrep` against a project with no
existing index, the search transparently falls back to rg in < 1 s
while a background worker indexes — but the user doesn't know that
and may think the tool is broken or slow. `0.2.4` adds a one-time
three-line greeting:

```
$ skygrep "where is the cascade tau threshold defined"
👋 First time in this project — skygrep is auto-indexing in the background.
   This first query falls back to rg in <1 s; semantic queries follow as the index builds.
   Try `skygrep doctor` for a health check, or `skygrep setup` to register with your LLM CLI.
```

Recorded in the `metadata` table (key `first_run_nudge_shown`) so
subsequent queries in the same project don't repeat. Suppressed in
JSON-output mode so machine consumers don't see chatter on stderr
that gets merged with stdout in some test harnesses.

## Disable everything

Set `SKYGREP_NO_HINTS=1` to silence all four hint paths at once.
Also turns off the recovery worker's user-visible notice line.

## New module

`skylakegrep.src.intelligent_cli` centralises:

  - `detect_out_of_scope(query) -> dict | None`
  - `render_out_of_scope_hint(hint, query) -> str`
  - `closest_match(typed, candidates) -> str | None`
  - `suggest_for_unknown_command(typed, known) -> str | None`
  - `suggest_for_unknown_option(typed, known) -> str | None`
  - `assess_result_quality(results, telemetry) -> str | None`
  - `should_show_first_run_nudge(conn) -> bool`
  - `mark_first_run_nudge_shown(conn) -> None`
  - `render_first_run_nudge() -> str`
  - `hints_disabled() -> bool`

All cheap to import (no `requests`, no embedder). 21 new unit
tests in `tests/test_intelligent_cli.py` lock in the trigger
conditions; the test suite is now 155 / 155 passing.

## Compatibility

- Python ≥ 3.9 (unchanged)
- Existing 0.2.0–0.2.3 indexes: no migration; `metadata` table is
  shared with the recovery worker
- Bench numbers unchanged from 0.2.3: 30 / 30 across Django + React
  + Tokio at ~14.6 s/q aggregate

## Known follow-ups (not in 0.2.4)

- `skygrep tour` — interactive 5-step walkthrough for true first-time
  users. Slated for 0.2.5.
- Subcommand-typo detection for the bare form (currently `skygrep
  serach "..."` is treated as a query). Trade-off: would suppress
  some legitimate queries that happen to start with a near-subcommand
  word.
- Phase C: tracked in
  [`docs/plans/2026-05-05-phase-c-audit.md`](plans/2026-05-05-phase-c-audit.md)
  + the subagent's counter-ranking in
  [`docs/plans/2026-05-05-phase-c-exploration.md`](plans/2026-05-05-phase-c-exploration.md)
  (paths D / E / F surfaced).
