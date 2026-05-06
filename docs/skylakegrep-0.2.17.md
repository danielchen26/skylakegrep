# skylakegrep 0.2.17 — lifted dark theme · cheatsheet & config visualised · "custom type" rename

This is a **documentation-surface release**, not a code-path release.
Zero behaviour change, byte-compatible 0.2.16 indexes, same wheel
surface, same CLI, same JSON contract.

Three reviewer-flagged gaps from 0.2.16 fixed:

## 1. Lifted dark theme — slate-blue base, more visible glass effect

The 0.2.16 palette read as "still too dark — can the frosted glass
feel more *modern UI*?" Adjusted the entire surface up one notch:

  - `--bg`: `#0a0d12` (near-black) → `#13192a` (slate-blue)
  - `--bg-2`: `#0f131a` → `#181f33`
  - `--panel*`: opacity tokens bumped 1.6×–2.5× so panels feel lifted
    instead of flat.
  - `--panel-deep`: `#06080c` → `#0e1320`
  - `.themed-card` background opacity dropped to 62–72% (from 82%) so
    the slate-blue ambient orbs in the page background read through
    the frosted panel — true Liquid-Glass aesthetic, not solid
    "near-black + line border".
  - `backdrop-filter: blur(24px) saturate(1.1)` → `blur(36px) saturate(1.2)`.
  - All 6 SVG assets (`hero-dark`, `og-image`, `comparison-matrix`,
    `performance-matrix`, `workflow-diagram`, `content-types`) had
    their deep-black bg gradient stops lifted from `#070910` /
    `#07090d` → `#0e1320` and the slate stop from `#0a0d12` →
    `#13192a`. Glass-fill rgba opacity bumped ~50% across the cards
    so the glass actually feels translucent instead of "solid panel
    with cyan border". `og-image.png` re-rasterized.

Net effect: the dark *is* dark, but no longer pure black. Panels
feel like real frosted glass over a slate-blue depth field.

## 2. Two more themed SVG cards — CLI cheatsheet · Configuration

The 0.2.16 README still had two surfaces in plain markdown:
**Command cheatsheet** and **Configuration**. Both now anchored
by themed SVG cards in the same Liquid-Glass aesthetic:

  - **`docs/assets/cli-cheatsheet.svg`** — hero featured tile for
    the bare form `skygrep "<query>"` (95% of real use), then 8
    secondary commands as 4×2 tiles:
    - row 1 (cyan accents): `search` · `doctor` · `setup` · `stats`
    - row 2 (violet & mint accents): `index` · `watch` · `serve` ·
      `enrich`
  - **`docs/assets/configuration.svg`** — three grouped panels with
    distinct accent colors:
    - cyan: **Ollama setup** (`OLLAMA_URL` · `EMBED_MODEL` ·
      `LLM_MODEL` · `HYDE_MODEL` · `KEEP_ALIVE`)
    - violet: **Indexing & rerank** (`SKYGREP_DB_PATH` · `AUTO_PULL`
      · `AUTO_REFRESH_THROTTLE_SECONDS` · `RERANK_MODEL` ·
      `RERANK_POOL`)
    - mint: **Behavior toggles** (`SKYGREP_NO_HINTS` · `NO_PROACTIVE`
      · `PROACTIVE_BUDGET_MS` · `FOOTER_COMPACT`)

Plain-markdown tables stay below each SVG inside `<details>` blocks
as keyboard / screen-reader fallback. The README is now four
themed-SVG anchors deep: comparison, performance, content types,
**cheatsheet, configuration**, plus the workflow diagram.

## 3. "Custom type" tile dropped — content-agnostic doesn't mean "you must customize"

The 0.2.16 `content-types.svg` had a 6th tile labelled "Your
custom type" sitting alongside Code / Markdown / PDF / Word docs /
Plain text family. Reviewer feedback: *"why does it say customize?
Isn't this content-agnostic? I have to customize?"* — fair point.
The 5 built-in types cover ~all practical formats; the
`register_extractor()` API is **extensibility**, not a content type
the user has to provide.

Restructured:

  - 5 built-in tiles (3 + 2 layout) instead of 6 — Code, Markdown,
    PDF, Word docs, Plain text family.
  - A wide footer banner with mint-cyan-violet gradient:
    *"Other formats? `register_extractor()` plugs any new content
    type into the same pipeline — that's what content-agnostic
    means."*

Now the sales pitch reads correctly: **5 types work out of the box;
extensibility is a property of the substrate, not a chore for the
user.**

## Compatibility

  - Python: unchanged — 3.9+
  - Default embedder / LLM router: unchanged — `bge-m3` / `qwen2.5:3b`
  - Wheel surface: unchanged
  - Index format: unchanged — byte-compatible with 0.2.16
  - JSON output schema: unchanged

## Bench numbers

Unchanged from 0.2.16. 30 / 30 public-OSS recall holds.

## Eight-surface checklist

  - [x] `pyproject.toml` 0.2.16 → 0.2.17
  - [x] `docs/skylakegrep-0.2.17.md` (this file)
  - [x] `README.md` — `cli-cheatsheet.svg` + `configuration.svg`
        anchored above the markdown tables; v0.2.17 in pill text
  - [x] `docs/index.html` — comparison `v0.2.16` → `v0.2.17`;
        `.themed-card` background lifted to slate-blue, blur
        bumped to 36px
  - [x] `docs/styles.css` — `--bg`, `--bg-2`, `--panel*`,
        `--panel-deep` lifted from near-black to slate-blue
  - [x] `docs/assets/content-types.svg` — fully rewritten: 5 tiles
        + extensibility footer banner (was 6 with confusing custom
        tile)
  - [x] `docs/assets/cli-cheatsheet.svg` (new)
  - [x] `docs/assets/configuration.svg` (new)
  - [x] `docs/assets/hero-dark.svg` v0.2.16 → v0.2.17, bg lifted,
        glass opacity bumped
  - [x] `docs/assets/og-image.svg` v0.2.16 → v0.2.17, bg lifted
  - [x] `docs/assets/og-image.png` re-rasterized
  - [x] `docs/assets/comparison-matrix.svg` + `performance-matrix.svg`
        + `workflow-diagram.svg` all bg lifted, glass-opacity bumped
  - [x] `docs/changelog.html` — 0.2.17 release card
  - [ ] GitHub repo description — unchanged
  - [x] PyPI upload (manual `twine`)
  - [x] GitHub Release with attached wheel + sdist
  - [x] `git tag -a v0.2.17` + push

## Acknowledgments

Three rounds of feedback in one session:

  1. *"You didn't compare with mgrep"* — fixed in 0.2.15.
  2. *"You got mgrep wrong — it's a paid Mixedbread cloud product"*
     — fixed in 0.2.16.
  3. *"The dark is still too dark — can it be lifted with more
     visible glass effect? And why does the diagram say customize?
     And the cheatsheet + config tables are still plain markdown"*
     — all three: fixed in this release.
