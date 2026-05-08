# skylakegrep 0.2.3 — release notes

A focused docs / advertise sync. **No code changes**, no behaviour
changes, no benchmark deltas. The release exists to fix a
discipline failure: the 0.2.2 features (intelligent background
recovery, routing transparency) were shipped to PyPI but never
surfaced on the public-facing GitHub Pages site or the README.
A new user landing on either surface had no way to find out the
features existed.

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact the maintainers.

## What changed

### 1. Comprehensive `0.2.2` advertise on the GitHub Pages site

`docs/index.html` gained two new "What's new in 0.2.x" cards
covering the 0.2.2 changes:

  - **`RECOVERY · 0.2.2` — Intelligent background re-embed.**
    Embedder upgrades used to require a 10–30 min blocking
    `skygrep index --reset`. Now: every search detects the dim
    mismatch, spawns a daemon-thread worker that re-embeds stale
    chunks in **mtime-DESC priority** (recently-modified files
    first — Pareto-distributed user behaviour means semantic
    coverage of likely queries reaches near-100 % within seconds).
    The user is never blocked; the existing
    `_filter_to_matching_dim` helper hides stale-dim chunks from
    the cascade. Crash-safe and resumable.

  - **`TRANSPARENCY · 0.2.2` — Routing path on every query.**
    The CLI's per-query telemetry footer leads with
    `path=cosine-cheap` / `cosine-escalated-rerank` / `rg-only`,
    the σ-evidence reason
    (`σ-gap=0.03 < τ → escalated to rerank`), the recovery state
    if a worker is active (`coverage=23% ETA=14m12s`), and a
    `quality=BEST` / `quality=DEGRADED-recovery` tag.

The existing `0.2.0` cards (substrate / reference graph / cascade
/ path filter / bench / latency) gained explicit `· 0.2.0` version
tags so the timeline is unambiguous. The "Where it pulls ahead of
`rg`, cloud RAG, or grep" comparison panel gained two new rows for
embedder-upgrade UX and routing transparency. The hero version
eyebrow is now `v0.2.3`.

### 2. New "Command cheatsheet" section (README + GitHub Pages)

A single table that lists every command, when to use it, and one
example, so a reader can see the entire surface at a glance instead
of digging through `--help` text:

| Command | When to use | Example |
| --- | --- | --- |
| `skygrep "<query>"` *(bare)* | Default. Auto-indexes, auto-recovers. | `skygrep "where is the auth refresh logic"` |
| `skygrep search <query>` | Explicit form when you need flags. | `skygrep search "session token" --top 20 --json` |
| `skygrep doctor` | First-time troubleshooting. | `skygrep doctor` |
| `skygrep setup` | Register with LLM CLIs. Run once. | `skygrep setup` |
| `skygrep stats` | Print chunk and file counts. | `skygrep stats` |
| `skygrep index [PATH] [--reset]` | Rarely needed. | `skygrep index . --reset` |
| `skygrep watch [PATH] -i N` | Keep index live. | `skygrep watch .` |
| `skygrep serve --port P` | Daemon mode. | `skygrep serve --port 7878` |
| `skygrep enrich` | Doc2query enrichment. | `skygrep enrich` |

The table is mirrored on the GitHub Pages "Reference · CLI"
section (was previously titled "Four commands" and listed only
four — we have nine). The page also gained a "First-time
workflow (5 commands, < 2 min)" walkthrough that takes a brand-new
user from install to first query in five visible steps.

### 3. New "Reading the per-query telemetry footer" guide

Both surfaces now explain field-by-field what the new
`path=` / `σ-gap=` / `recovery=` / `quality=` fields mean, so
power users can debug the cascade's decisions without reading
source.

### 4. New release checklist (`docs/RELEASING.md`)

The discipline failure that motivated this release happens because
the per-release update was implicit. `docs/RELEASING.md` makes it
an explicit checklist of every surface that has to be touched
(pyproject version, README sections, GitHub Pages cards / panels,
release-notes file, capability matrix, GitHub repo description,
PyPI upload, GitHub Release with artifacts). Future releases
should not be able to "ship to PyPI without telling anyone" again.

### 5. Stale defaults swept

  - `README.md` "## In 30 seconds" snippet: `ollama pull
    nomic-embed-text` → `ollama pull bge-m3` (the default since
    `0.2.0`); telemetry footer in the example updated to the new
    `0.2.2` field set.
  - `README.md` "## Configuration" `OLLAMA_EMBED_MODEL` row:
    default corrected from `nomic-embed-text` to `bge-m3`; the
    "Switching requires `--reset`" note rewritten to reflect the
    `0.2.2` auto-recovery behaviour.
  - Capability matrix gained four new rows (intelligent recovery,
    routing transparency, command cheatsheet, release checklist).

## Compatibility

- **Python**: ≥ 3.9 (unchanged)
- **Indexes**: no migration; `0.2.2` and `0.2.3` are byte-compatible
- **No code path changed.** `git log` shows only doc files in this
  release's diff, and 134 / 134 tests pass against an unchanged
  test surface.

## Bench numbers (unchanged from 0.2.2)

| Repo | Recall | Latency |
| --- | :-: | :-: |
| Django | 10 / 10 | 10.13 s/q |
| Tokio | 10 / 10 | 21.91 s/q |
| React | 10 / 10 | 11.71 s/q |
| **Aggregate** | **30 / 30 (100 %)** | **~14.6 s/q** |

## Known follow-ups (not in 0.2.3)

- **0.2.4** — intelligent CLI assistance: typo correction
  suggestions, out-of-scope query detection (e.g. "我最近 10 个文件"
  is a metadata query, not a content search; the system should
  notice and suggest `git log --name-only`), low-confidence
  empty-result hints, first-run nudge with the three canonical
  commands.
- **Phase C** — intelligent retrieval: tracked in
  [`docs/plans/2026-05-05-phase-c-audit.md`](plans/2026-05-05-phase-c-audit.md)
  with three candidate paths (A: code-specific symbol channel; B:
  content-agnostic structural-channel registry; C: smarter
  cascade-internal scheduler) and a corresponding exploration
  audit at
  [`docs/plans/2026-05-05-phase-c-exploration.md`](plans/2026-05-05-phase-c-exploration.md)
  that challenges the original ranking and surfaces three additional
  paths (D, E, F).
- Re-render `docs/assets/{benchmark,schema,hero-dark,og-image}.svg`
  to reflect the `bge-m3` defaults — visual assets, separate
  cosmetic pass.
- Re-run the self-test bench
  (`benchmarks/agent_context_benchmark.py`) on `bge-m3` and update
  `docs/token-benchmarking.md` top-k 5 row.
- Fix the GitHub Actions `PYPI_API_TOKEN` secret (currently 403).
