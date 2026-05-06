# skylakegrep 0.5.2 — themed HTML release notes + de-hardcoded proactive dirs

A doc-surface + small-config release — the two threads I left
unfinished after 0.5.0 / 0.5.1 are now closed:

## 1. Themed HTML release-notes pages for 0.5.0 and 0.5.1

0.5.0 and 0.5.1 shipped with `.md` release notes only — anyone
landing on `docs/changelog.html` saw "Full notes →" links that
404'd because the rendered `.html` pages didn't exist. Closing
this surface gap was on the eight-surface release checklist
(`docs/RELEASING.md`) and was missed twice.

Now under `docs/`:

- `skylakegrep-0.5.0.html`  ← rendered from `skylakegrep-0.5.0.md`
- `skylakegrep-0.5.1.html`  ← rendered from `skylakegrep-0.5.1.md`
- `skylakegrep-0.5.2.html`  ← this page

A reusable renderer (`scripts/render_release_notes.py`) wraps the
markdown in the same themed sidebar / topbar layout as 0.4.2.html
so future releases don't repeat the lapse:

```bash
.venv/bin/python scripts/render_release_notes.py 0.5.X
```

The script uses `markdown` (already a dev-time dependency for the
docs build) and reads from `docs/skylakegrep-X.Y.Z.md` directly.

## 2. `SKYGREP_PROACTIVE_DIRS` — replaces the hardcoded user-personal folder list

Two functions in 0.5.x had the same anti-pattern: a list of the
maintainer's personal directories baked into source code.

- `proactive._default_search_dirs()` — used by `filename_extend`
  to look beyond the project root for filename queries:
  hardcoded `~/Downloads`, `~/Desktop`, `~/Documents`.
- `lazy_indexer.lazy_explore_cross_folder()` — used by the API
  for semantic cross-folder exploration: hardcoded the same
  three plus `~/Pictures`, `~/Code`, `~/Projects`.

Neither list is universal. `~/Code` and `~/Projects` only exist
for users who follow that convention (the maintainer happens to);
many users keep their work under `~/repos`, `~/src`, `~/work`,
`/data/projects`, etc. Hardcoding either pattern creates the
exact wrong default for everyone else.

0.5.2 makes both honor `SKYGREP_PROACTIVE_DIRS`, a colon-separated
list of absolute paths:

```bash
# Override for this shell
export SKYGREP_PROACTIVE_DIRS="$HOME/repos:$HOME/work:/data/projects"

# Or per-invocation
SKYGREP_PROACTIVE_DIRS="$HOME/src" skygrep "<query>"
```

Stale or missing dirs are silently filtered out so a partial config
doesn't waste scheduling budget on `find` walks against missing
trees. When the env var is unset, the legacy hardcoded list still
applies — no behaviour change for users who don't opt in.

This is a small change in lines but a meaningful one: the codebase
now contains zero personal-filesystem assumptions.

## What's still pending (0.6 candidates)

- A new proactive enhancer `cross_folder_semantic` that wraps
  `lazy_explore_cross_folder` so the warm-cascade path (not just
  cold-start) can fall back to sibling-folder semantic search when
  in-project results are weak. The wiring requires careful
  individual_budget_ms tuning for the embedder call, which is
  bigger than belongs in a 0.5.x patch — deferred to 0.6.
- `--lazy/--no-lazy` is currently only consulted on the cold-start
  branch. Wiring it into the warm cascade as an "if cascade is
  weak, also try lazy in cwd" augmentation is the next step.

## Verified

- `pytest tests/` — 201 / 201 pass
- `_default_search_dirs()` smoke test:
  ```python
  os.environ['SKYGREP_PROACTIVE_DIRS'] = '/tmp:/var/tmp:/nonexistent'
  → ['/tmp', '/var/tmp']     # nonexistent dropped
  ```
- 0.5.0 / 0.5.1 / 0.5.2 themed HTML pages rendered and linked from
  `docs/changelog.html`.
- 0.5.0 / 0.5.1 PyPI installs verified end-to-end on a fresh
  Django checkout in 0.5.1 release notes.
