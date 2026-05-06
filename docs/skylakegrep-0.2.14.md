# skylakegrep 0.2.14 — homepage redesign

This release is a **documentation-surface release**, not a code-path
release. Zero behaviour change. Existing installs do not need to
rebuild any indexes, refresh any caches, or re-pull any models.
Same wheel surface, same CLI, same JSON contract.

What changed: the GitHub Pages site and the `README.md` were stacked,
unfocused, and didn't pass the five-second test. A new visitor had no
way to answer *what is this, is it for me, why should I care* without
scrolling through 1,400 lines of dense reference docs.

This release rebuilds the front door.

## What's new

### A new home page

`docs/index.html` is now a focused landing page (~850 lines, was
1,405). It tells the story in seven beats:

  1. **Hero** with an animated terminal demo — three queries cycle
     through (auth-token refresh, retry-with-backoff, JWT expiration)
     so a visitor sees the *kind of question* skygrep answers, not
     just a feature list.
  2. **Three scenarios** — code by concept, cross-content (PDF +
     code + markdown together), multilingual & private. The
     audience-selector that the old site lacked.
  3. **Why skylakegrep?** — six-row comparison table against
     `ripgrep` and Cloud RAG (Cursor, Sourcegraph, …). The first
     surface that frames the actual landscape.
  4. **How it works** — three-step horizontal flow: LLM router →
     cosine cascade → proactive layer. Each step names the model
     and its latency budget so the architecture is legible at a
     glance.
  5. **Install + Quickstart** — compacted to the minimum four
     commands.
  6. **Benchmarks headline** — three big numbers (30/30 OSS recall ·
     60×–770× less context · −82% tool calls in agent loops) with a
     single CTA into the full benchmarks page.
  7. **Honesty** — what the data does and does NOT show, in four
     bullet points. Sample sizes, latency caveats, scope limits.

### Five new subpages

The deep-reference content moved out of the home page and into
purpose-built subpages with shared chrome (topbar + sidebar, no
right-TOC clutter):

  - `docs/concepts.html` — indexing · ranking · output mechanics
  - `docs/architecture.html` — local Ollama + SQLite + ranking pipeline
  - `docs/cli.html` — full CLI cheatsheet + configuration env vars
  - `docs/reference.html` — JSON output schema (the agent contract)
  - `docs/benchmarks.html` — all four sub-benchmarks + worked example
  - `docs/changelog.html` — release-card grid linking to per-version
    notes

### A rewritten `README.md`

The GitHub README is now a focused landing surface, not a wall of
release tables. It opens with the same hero, the same three
scenarios, the same comparison table, the same how-it-works diagram
that the GitHub Pages site uses, then condenses to a four-command
install + a performance highlight + a command cheatsheet + a
condensed "what's new" pointer.

The release-history table that previously consumed 60% of the README
moved to `docs/changelog.html`.

## Why this matters

A new visitor decides whether your project is for them in the first
five seconds. Before this release:

  - The first paragraph was dense reference text.
  - There was no comparison surface — no answer to "I already use
    ripgrep / Cursor, why would I install this?"
  - There was no how-it-works diagram — the architecture was buried
    20+ sections in.
  - The benchmark headline was buried beneath four sub-benchmarks
    each with their own caveats.

After this release, every one of those questions gets answered above
the fold.

## Compatibility

  - **Python**: unchanged — 3.9+
  - **Default embedder**: unchanged — `bge-m3`
  - **Default LLM router**: unchanged — `qwen2.5:3b`
  - **Wheel surface**: unchanged — same `skygrep` entry point, same
    flags, same env var contract
  - **Index format**: unchanged — `~/.local-mgrep/<repo-id>/index.db`
    is byte-compatible with 0.2.13
  - **JSON output schema**: unchanged

## Bench numbers

Unchanged from 0.2.13 — this release ships zero ranking-path code.

  - Public-OSS recall: **30 / 30** on Django + React + Tokio
  - Agent loops: **−37.6%** single-turn, **−82%** best-case multi-turn
    tool calls vs. `rg`-only
  - Worst-case latency: bge-m3 inference is +57% per-query slower than
    mxbai on Tokio specifically; the three-repo average is still −19%.

## Eight-surface checklist

  - [x] `pyproject.toml` 0.2.13 → 0.2.14
  - [x] `docs/skylakegrep-0.2.14.md` (this file)
  - [x] `README.md` (full rewrite)
  - [x] `docs/index.html` (full rewrite)
  - [x] `docs/concepts.html` + `architecture.html` + `cli.html` +
        `reference.html` + `benchmarks.html` + `changelog.html` (new)
  - [x] `docs/RELEASING.md` (extended to cover the new pages)
  - [ ] GitHub repo description — unchanged (positioning is the same)
  - [x] PyPI upload — manual `twine upload` flow (CI 403 still
        unfixed; tracked separately)
  - [x] GitHub Release with attached wheel + sdist
  - [x] `git tag -a v0.2.14` + push

## Known follow-ups

  - The CI workflow still 403s on `PYPI_API_TOKEN`. Manual `twine`
    flow remains the working path; the secret-fix is tracked
    separately and not gating this release.
  - Sub-page content was lifted verbatim from the previous
    `docs/index.html`. A second pass to tighten copy on each subpage
    is a candidate follow-up.
  - The animated hero terminal demo respects
    `prefers-reduced-motion`; the static fallback shows only frame 1.
    A future iteration could add a "Pause animation" affordance.

## Acknowledgments

This release was driven by direct feedback that the homepage was
"just stacked content" with no five-second pitch. The redesign plan
lives at `docs/plans/2026-05-05-homepage-redesign.md`.
