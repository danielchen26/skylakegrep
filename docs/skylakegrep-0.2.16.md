# skylakegrep 0.2.16 — mgrep correction · softer palette · workflow + content-types upgraded

This is a **documentation-surface release**, not a code-path release.
Zero behaviour change, byte-compatible 0.2.15 indexes, same wheel
surface, same CLI, same JSON contract.

Three reviewer-flagged gaps from 0.2.15 fixed:

## 1. mgrep correction — it's a paid Mixedbread CLI, not a "predecessor"

The 0.2.15 comparison labeled `mgrep` as a "legacy substrate /
predecessor" — that was wrong. The real
[`mgrep`](https://www.mgrep.dev/) is by
[Mixedbread AI](https://www.mixedbread.com/) (the team behind
`mxbai-embed-large`), a TypeScript / npm CLI that semantic-greps
local files via a **cloud-backed** Mixedbread store with browser
authentication and a **paid subscription tier** (free-tier ingest
is capped at 2,000 tokens). It's actively developed and is the
closest commercial competitor to skylakegrep, not a predecessor.

Corrected across the comparison surface:

  - **Concept search**: ✓ (was "legacy substrate")
  - **Privacy**: ✗ cloud-backed (was ✓)
  - **Content**: code · text · PDF · images (multimodal — actually
    a strength)
  - **Setup**: npm + Mixedbread account (was `pip install`)
  - **Cost**: subscription + usage-based (was $0/mo)
  - **Multilingual**: cloud embedder (Mixedbread-hosted), not
    "English-leaning"

The honest framing now: same query surface as skylakegrep, opposite
trade-offs — **skylakegrep is fully local; mgrep is hybrid local /
cloud with a paid tier.** Either could be the right pick depending
on whether you can ship code to a cloud-hosted store and pay a
subscription.

## 2. Softer dark frosted-glass palette — less neon, more refined

The 0.2.15 palette read as "too dark + too neon-cyan". Adjusted:

  - `--accent-bright`: `#67e8f9` (Tailwind cyan-300, vibrant) →
    `#a5f3fc` (Tailwind cyan-200, pastel) — softer text accent.
  - `--accent`: `#22d3ee` (cyan-400, neon) → `#38bdf8` (sky-400,
    less neon).
  - All `rgba(34, 211, 238, *)` → `rgba(125, 211, 252, *)` with
    opacity reduced ~12 % — sky-blue tones at slightly lower
    intensity.
  - `.themed-card` chrome rebuilt with **multi-stop translucent
    border** (cyan → violet → mint, same as the hero
    `glass-stroke`) plus `backdrop-filter: blur(24px) saturate(1.1)`
    so the panel feels like real frosted glass, not a solid card.
  - Result-card filename / pill stroke colors throughout the four
    SVG assets (`hero-dark.svg`, `og-image.svg`,
    `comparison-matrix.svg`, `performance-matrix.svg`) all moved
    to the softened palette — 8 unique tokens replaced in
    `hero-dark.svg`, 4 in comparison, 5 in performance, 3 in og.

## 3. Two new themed SVG cards — workflow + content types

The README still had two surfaces in the "old / unstyled"
aesthetic:

  - The **how-it-works** section was an ASCII-art box-drawing
    diagram (`┌──┐ → ┌──┐ → ┌──┐`).
  - The **what-you-can-search** section was a plain markdown
    table.

Both replaced with new themed SVG cards:

  - `docs/assets/workflow-diagram.svg` — three-stage pipeline
    rendered as three frosted-glass cards (LLM router · cosine
    cascade · proactive layer) with cyan / violet / mint per-step
    halos, soft arrows between stages, and a "Local Ollama · Local
    SQLite · Zero network · Zero subscription" footer strip. The
    same design language as `hero-dark.svg`.
  - `docs/assets/content-types.svg` — six content tiles in a 3 × 2
    grid (Code, Markdown, PDF, Word docs, Plain text family, Your
    custom type). Each tile color-coded (cyan / violet / mint) by
    "category".

Both anchor the README; the plain markdown table stays inside a
`<details>` block as fallback.

## Compatibility

  - Python: unchanged — 3.9+
  - Default embedder: unchanged — `bge-m3`
  - Default LLM router: unchanged — `qwen2.5:3b`
  - Wheel surface: unchanged
  - Index format: unchanged — byte-compatible with 0.2.15
  - JSON output schema: unchanged

## Bench numbers

Unchanged from 0.2.15 — this release ships zero ranking-path code.
30 / 30 public-OSS recall holds.

## Eight-surface checklist

  - [x] `pyproject.toml` 0.2.15 → 0.2.16
  - [x] `docs/skylakegrep-0.2.16.md` (this file)
  - [x] `README.md` — mgrep wording corrected · ASCII workflow →
        SVG · content-types table → SVG · v0.2.16 in pill text
  - [x] `docs/index.html` — comparison row corrected · themed-card
        rebuilt with multi-stop border + backdrop-filter
  - [x] `docs/styles.css` — palette tokens moved to softer sky /
        pastel range
  - [x] `docs/assets/workflow-diagram.svg` (new)
  - [x] `docs/assets/content-types.svg` (new)
  - [x] `docs/assets/hero-dark.svg` v0.2.15 → v0.2.16, palette
        softened
  - [x] `docs/assets/og-image.svg` v0.2.15 → v0.2.16, palette
        softened
  - [x] `docs/assets/og-image.png` re-rasterized
  - [x] `docs/assets/comparison-matrix.svg` mgrep row corrected,
        v0.2.16, palette softened
  - [x] `docs/assets/performance-matrix.svg` v0.2.16, palette
        softened
  - [ ] GitHub repo description — unchanged
  - [x] PyPI upload (manual `twine`)
  - [x] GitHub Release with attached wheel + sdist
  - [x] `git tag -a v0.2.16` + push

## Acknowledgments

This release was driven by direct feedback in three rounds:

  1. "First, you didn't compare with mgrep" — fixed by adding a
     named-tool comparison in 0.2.15.
  2. "You got mgrep wrong — it's a paid product, not legacy" —
     fixed in this release with the corrected metadata.
  3. "The colors are too dark and the cyan a bit too neon —
     can you do a softer dark frosted-glass version?" — fixed by
     the palette migration above.
