# skylakegrep 0.2.15 — comparison matrix + visual unification

This is a **documentation-surface release**, not a code-path
release. Zero behaviour change, same wheel surface, same CLI, same
JSON contract. Existing 0.2.14 indexes work unchanged.

What changed: the 0.2.14 homepage redesign was correct in structure
but had two visual gaps a reviewer caught quickly:

  1. The comparison surface compared skylakegrep against generic
     "Cloud RAG (Cursor, Sourcegraph, …)" — a category, not a named
     tool. There was no comparison against `mgrep` (the predecessor)
     or against the closest direct competitor in the offline-Ollama-CLI
     lane.
  2. The hero image and the comparison / performance tables on the
     README didn't share a visual theme. The hero is a frosted-glass
     terminal card; the tables were plain markdown tables. The eye
     bounced between two visual languages.

This release fixes both.

## What's new

### A comparison sized against named tools

The comparison surface now sizes skylakegrep against **four named
alternatives**, not generic categories:

  - **`ripgrep`** — the lexical baseline.
  - **`mgrep`** — the predecessor. skylakegrep is the next-generation
    evolution with the bge-m3 substrate, content-agnostic reference
    graph, and σ-adaptive cascade. mgrep is recorded honestly as a
    legacy substrate with English-leaning behaviour.
  - **[`autodev-codebase`](https://github.com/anrgct/autodev-codebase)** —
    the closest direct competitor in the offline-Ollama-CLI lane.
    Vector-embedding semantic search, MCP server + CLI, Ollama
    integration. Apples-to-apples on the privacy + offline + concept-
    search axes; skylakegrep wins on multi-content (code · md · PDF
    · docx) and on multilingual queries (bge-m3 substrate).
  - **Sourcegraph Cody** — the cloud commercial reference point.
    The honest answer to "why not just use a cloud agent?"

The six rows are: concept-find · privacy · content types · setup
friction · cost · multilingual queries. Each cell is a real claim,
not a checkmark — *legacy substrate*, *English-leaning*, *embedder-
dependent* are surfaced where appropriate. The footer reminds the
reader: "positions are descriptive, not endorsements."

### A visual language that matches the hero

Two new SVG surfaces — both rendered in the same frosted-glass
terminal-card aesthetic as the hero:

  - **`docs/assets/comparison-matrix.svg`** — the comparison rendered
    as a designed card with macOS traffic lights, the cyan/violet/
    emerald glass-stroke gradient, the self-column highlight, and
    inline ✓ / ✗ / partial typography that matches the hero result
    cards.
  - **`docs/assets/performance-matrix.svg`** — the three-repo
    benchmark rendered as three accent-tinted stat cards (10/10
    each) plus an aggregate banner (30/30 · 60×–770× · −82%).
    Same dark gradient, same chrome.

These now anchor the README. The plain-markdown tables stay below
each SVG inside `<details>` blocks for keyboard / screen-reader
fallback.

### docs/index.html: comparison restyled in-place

The `<table>` markup was replaced with a CSS-grid card that uses
the same `themed-card` chrome the hero terminal uses — traffic
lights, dark gradient bg, frosted border, soft shadow. The
self-column gets a subtle accent gradient. Mobile breakpoint
collapses to a single column with each capability as a sticky
section header.

### Hero version bumped

The version pill on `hero-dark.svg`, the install-line version on
`og-image.svg`, and the re-rasterized `og-image.png` (1200×630) all
now show **v0.2.15**. The previous release shipped with v0.2.13 still
visible on the rendered hero — caught by the reviewer.

## Compatibility

  - Python: unchanged — 3.9+
  - Default embedder: unchanged — `bge-m3`
  - Default LLM router: unchanged — `qwen2.5:3b`
  - Wheel surface: unchanged
  - Index format: unchanged — byte-compatible with 0.2.14
  - JSON output schema: unchanged

## Bench numbers

Unchanged from 0.2.14 — this release ships zero ranking-path code.
30 / 30 public-OSS recall holds.

## Eight-surface checklist

  - [x] `pyproject.toml` 0.2.14 → 0.2.15
  - [x] `docs/skylakegrep-0.2.15.md` (this file)
  - [x] `README.md` — comparison + perf tables replaced with themed
        SVG cards; named-tool comparison row updated
  - [x] `docs/index.html` — `.why-table` replaced with
        `.themed-card` + 5-column CSS grid; `.themed-card` chrome
        added as a reusable pattern
  - [x] `docs/assets/comparison-matrix.svg` (new)
  - [x] `docs/assets/performance-matrix.svg` (new)
  - [x] `docs/assets/hero-dark.svg` v0.2.13 → v0.2.15
  - [x] `docs/assets/og-image.svg` v0.2.13 → v0.2.15
  - [x] `docs/assets/og-image.png` re-rasterized
  - [ ] GitHub repo description — unchanged
  - [x] PyPI upload (manual `twine`)
  - [x] GitHub Release with attached wheel + sdist
  - [x] `git tag -a v0.2.15` + push

## Known follow-ups

  - The `.themed-card` chrome (traffic lights + dark gradient) is
    reusable. A future polish pass could apply it to other tables
    (`docs/cli.html` cheatsheet, `docs/architecture.html` schema,
    `docs/benchmarks.html` per-repo breakdowns) for full visual
    unification across the doc site.
  - The README still has two markdown tables that didn't get the
    SVG treatment ("What you can search" content-type table and
    the command cheatsheet). They're fine as plain markdown, but
    a future pass could convert them too.

## Acknowledgments

This release was driven by direct feedback flagging that the
comparison didn't include `mgrep`, that the tables didn't share
a visual theme with the hero image, and that the hero version
detail hadn't been updated. All three: fixed.
