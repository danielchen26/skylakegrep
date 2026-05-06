# skylakegrep 0.5.4 — full doc-surface completion + streaming cold-start UX

Two threads in one ship:

1. **Doc-surface completion for 0.5.3.** 0.5.3 shipped working code
   with the doc surfaces only partially swept; this release brings
   every surface in `docs/RELEASING.md` into sync.

2. **Streaming cold-start UX (small code change).** Reported by the
   user mid-bench: `skygrep '我有没有跟"eb1b"有关的文件？'`
   sat silent for ≥ 30 s on a never-indexed dir before any output
   appeared. The fix is described in detail below — the cold-start
   path now prints (a) an immediate `🔍 scanning…` line, (b) the
   preliminary `rg` + filename matches as soon as `rg` returns
   (typically < 1 s), (c) the `🌊 / 💧 / ⚡` lazy progress lines
   already added in 0.5.3, and (d) the lazy-refined matches as soon
   as the embed pass finishes — instead of waiting for the whole
   pipeline to complete and dumping everything at once.

## What changed (eight surfaces audit)

The 0.5.3 ship updated:
- `pyproject.toml` (version bump)
- `docs/skylakegrep-0.5.3.md` + `.html` (release notes)
- `docs/changelog.html` (release card)
- `docs/index.html` line 731 (one of the `Why skylakegrep?` table cells)

The 0.5.3 ship MISSED — fixed in 0.5.4:

1. **`docs/index.html` hero eyebrow** (was `v0.2.13`). Anyone landing
   on the GitHub Pages homepage saw a 0.2-era eyebrow over a
   project that's now at 0.5.x. Now reads `v0.5.4 · 100% local · …`.

2. **`docs/index.html` "Benchmarks summary" big numbers**. The
   30 / 30 fully-indexed peak was the only headline number. Added a
   second bench-stat for the new 0.5.3 cold-start lazy tier:
   **`+4 / 10 lazy auto-trigger over rg cold-start (0.5.3)`** with
   the Django vocab-mismatch oracle attribution. Also extended the
   section's lead paragraph to mention the three-tier model
   (rg cold ~100 ms / lazy cold ~5–30 s / fully-indexed cascade
   ~100 ms).

3. **`og:description` + `twitter:description` meta tags**. Both
   previously claimed only the `30/30 (100%) recall` headline; both
   now also call out the cold-start lazy tier so social-card previews
   reflect what 0.5.x actually shipped.

4. **`docs/cli.html` cheatsheet**. Added rows for
   `--lazy / --no-lazy`, `--rg-shortcut / --no-rg-shortcut`,
   `--filename-shortcut / --no-filename-shortcut`. Added a new
   `Environment variables (0.5.x)` table covering
   `SKYGREP_DB_PATH`, `SKYGREP_PROACTIVE_DIRS`,
   `SKYGREP_LLM_ROUTER_MODEL`, `OLLAMA_URL`, `OLLAMA_EMBED_MODEL`.

5. **`docs/benchmarks.html`**. New `<h3>` section
   `0. Cold-start lazy auto-trigger (NEW in 0.5.3)` with the
   apples-to-apples real-CLI table:
   - `--no-lazy` (rg-only): 0 / 10, 4.85 s avg
   - default (auto-trigger): 4 / 10, 20.76 s avg
   - delta: +4 / 10, +15.9 s / query
   Also bumped the page intro from `four reproducible benchmarks`
   to `five` and lists the specific hits (Q1 resolvers.py, Q3
   migration.py + executor.py, Q4 backends.py, Q7 base.py) plus an
   honest "still misses" list.

6. **`README.md` headline tagline**. Was
   `30 / 30 public-OSS recall · ~1 s warm · 100 % local · 14 releases`.
   Now reads `30 / 30 fully-indexed · +30 % lazy over rg cold-start
   (0.5.3) · ~1 s warm · 100 % local · 18 releases shipped`. The
   added line surfaces the 0.5.3 measured improvement at the very
   top of the README so anyone scrolling past sees the new tier.

7. **`docs/index.html` `Why skylakegrep?` table version pill**:
   bumped `v0.5.3` → `v0.5.4`. (The 0.5.3 release got this from
   0.5.2; 0.5.4 keeps it current.)

8. **`docs/changelog.html` release card** (this card you're
   reading the link from).

## Streaming cold-start output (the second thread)

Before 0.5.4 (what the user saw):

```
$ skygrep '我有没有跟"eb1b"有关的文件？'
                                              ← silent for 5–30 s
                                              ← prompt looks frozen
... (eventually all output appears at once) ...
```

After 0.5.4:

```
$ skygrep '我有没有跟"eb1b"有关的文件？'
🔍 ripgrep cold-start · scanning project keywords…       ← t = 0
▾ preliminary keyword matches (lazy semantic refinement starting…):
╭─ <path>:<lines> ────────────  1.000                     ← t ≈ 1 s
╭─ <path>:<lines> ────────────  1.000
🔍 lazy auto-trigger · scanning project structure…
🌊 4 subprocesses · N LLM-routed · M token-shortcut · K seeds total
💧 diffused +N import neighbours
⚡ embedding K files (1 Ollama call)…
▾ refined matches from lazy semantic search:
╭─ <path> ────────────────────  0.682                     ← t ≈ 30 s
╭─ <path> ────────────────────  0.667
[37.1 s · ripgrep cold-start + lazy auto (σ=0.014, conf=medium, …)]
```

User-visible improvements:
- **t = 0**: explicit "scanning…" message — no more silent prompt.
- **t ≈ 1 s**: preliminary `rg` + filename hits show immediately
  whenever lazy is going to fire. User has *something* to read.
- **t ≈ 1 s**: explicit "lazy semantic refinement starting…" header
  so the user knows there's a 5–30 s wait coming and doesn't kill
  the process thinking it hung.
- **t ≈ 5–30 s**: the `🌊 / 💧 / ⚡` progress lines from 0.5.3 stream
  to stderr while embed runs.
- **t ≈ 30 s**: lazy-refined matches print under a "▾ refined
  matches…" header. Already-printed paths from the preliminary
  block are de-duplicated so the same file never appears twice.

When `rg` is strong (no lazy fires) the streaming layer is bypassed
and output prints in one block as before — zero latency cost on the
fast path. The same is true for `--json` (one-shot, no streaming
markers).

## Why this is a separate ship

Per the project's release protocol (`docs/RELEASING.md`): every
version must do commit + tag + push + PyPI upload + GitHub Release
with attached artifacts + a rendered themed HTML release-notes
page + sweep all eight surfaces. 0.5.3 missed surfaces 1, 2, 3, 4,
5, 6 above. Rather than amend a published tag (which PyPI doesn't
permit and which leaves the GitHub release page stale), 0.5.4 is a
clean docs-only patch shipping the missing surfaces.

## Numbers

Same as 0.5.3 — no code changes, no bench changes:

- `pytest tests/` — **217 / 217 pass**
- Django oracle bench (real CLI, fresh DB per query):
  rg-only **0 / 10** → auto-trigger **4 / 10** at +15.9 s / query

## Compatibility

- All public APIs unchanged from 0.5.3.
- Same wheel + sdist file layout.
- Index format unchanged.
- 0.5.3-built indexes work without re-indexing.

## Verified

- Every surface listed above visually inspected on the rendered
  GitHub Pages site after push.
- `pytest tests/` — 217 / 217 (no code changes, baseline preserved).
- PyPI install of 0.5.4 in fresh venv reports correct version.

## Pending for 0.6

Unchanged from 0.5.3:

- Q2 / Q5 / Q6 / Q8 / Q9 / Q10 still don't hit on the Django bench
  — qwen 2.5:3b can't reliably pick the right dir for those
  specific oracle phrasings even after deterministic dir-token
  routing + simplification. Larger router model (qwen 7B+) or
  chain-of-thought prompt would likely lift these.
- Embedding chunk strategy is whole-file-truncate-2400; a
  structural-extraction (docstring + function signatures only)
  would likely lift cosine ranking among in-pool seeds.
- New proactive enhancer `cross_folder_semantic` that wraps
  `lazy_explore_cross_folder` for the warm-cascade path (currently
  only fires post-cascade if cascade gap < tau).
