# skylakegrep 0.2.20 — hero card geometry fix · pill row stable in raster too

This is a **documentation-surface release**, not a code-path release.
Zero behaviour change, byte-compatible 0.2.19 indexes, same wheel
surface, same CLI, same JSON contract.

## What changed

A reviewer screenshot of the live hero card on the GitHub Pages site
showed two visible defects:

  1. The **third result row** (`utils/jwt.py :22-39`) had its line-
     range text positioned at `x=100`, but `utils/jwt.py` (12 chars
     in the 12.5 px monospace) occupies `x ≈ 16 → 106`. The colon
     and the path collided — visually rendering as `utils/jwt.py22-39`.
  2. The **third result card** overflowed the terminal frame's
     bottom border by ~6 px. The card's height was 358 px; the body
     starts at `y=64` inside the card; result 3 lives at body-`y=220`
     with height 80, so it ended at card-`y=364` — 6 px below the
     358-px card edge.

Both fixed:

  - `utils/jwt.py` line-range moved from `x="100"` → `x="120"`,
    giving a +14 px gap (matches the +15 px gap on result-2's
    `auth/middleware.py` row).
  - Terminal card height bumped from 358 → 380 px; SVG `viewBox`
    height from 600 → 620; bg + vignette rects updated to match.
    Result 3 now ends at card-`y=364` with a clean 16 px bottom
    margin.

A static text-position sanity check now runs against the hero SVG:

```
✓ 'auth/refresh.py'   (15 chars) end≈128, ':' starts at 138, gap +9.5px
✓ 'auth/middleware.py' (18 chars) end≈151, ':' starts at 166, gap +15.0px
✓ 'utils/jwt.py'       (12 chars) end≈106, ':' starts at 120, gap +14.0px
card height = 380, result-3 bottom = 364, ✓ fits
```

## Two related XML / raster issues caught while sweeping

While doing the overall review (`整体看一下`), I caught and fixed
two more issues no one would have seen yet:

  - **`cli-cheatsheet.svg`** had an unescaped `<query>` literal in
    its `<desc>` element, which made cairosvg refuse to parse the
    file. Switched to `&lt;query&gt;` so the SVG is XML-valid and
    rasterisable.
  - **`configuration.svg`** had an unescaped `&` in `Indexing &
    rerank` inside its `<desc>`. Same problem, same fix:
    `&amp;`.

Neither showed in browser-rendered SVG (browsers are lenient), but
both broke the cairosvg PNG-rasterisation pipeline.

### Pill-row tspan stability

The hero brand block and the og-image both had a version-pill row
that mixed text content with leading/middle `<tspan>` elements
under `text-anchor="middle"`. cairosvg has a known limitation here
— it positions each tspan independently rather than as inline-flow,
producing visible overlap in the rasterised PNG (seen as
`PolyForm-NC-v0.2.20Python 3.9+`). Browsers render correctly per
SVG spec, but **social-preview crawlers (Twitter, Facebook,
LinkedIn) fetch the cached `og-image.png` directly** — so
rasterisation accuracy matters for unfurls.

Switched both pill rows to a single flat-text element with the
cyan accent applied to the whole line. Same information, identical
visual rhythm in browser and crawler renders. No more overlap.

## Compatibility

  - Python: unchanged — 3.9+
  - Default embedder / LLM router: unchanged
  - Wheel surface: unchanged
  - Index format: byte-compatible with 0.2.19
  - JSON output schema: unchanged

## Bench numbers

Unchanged from 0.2.19. 30 / 30 public-OSS recall holds.

## Eight-surface checklist

  - [x] `pyproject.toml` 0.2.19 → 0.2.20
  - [x] `docs/skylakegrep-0.2.20.md` (this file) + rendered
        `skylakegrep-0.2.20.html`
  - [x] `README.md` v0.2.20 in pill text
  - [x] `docs/index.html` v0.2.20 in why-grid header
  - [x] `docs/changelog.html` — 0.2.20 release card
  - [x] `docs/assets/hero-dark.svg` — card height 358→380, viewBox
        600→620, line-range x 100→120, pill row flattened to plain
        text
  - [x] `docs/assets/og-image.svg` — pill row flattened to plain
        text
  - [x] `docs/assets/og-image.png` re-rasterized
  - [x] `docs/assets/cli-cheatsheet.svg` — `<query>` escaped
  - [x] `docs/assets/configuration.svg` — `&` escaped
  - [x] All 6 SVG version pills bumped 0.2.19 → 0.2.20
  - [x] PyPI upload (manual `twine`)
  - [x] GitHub Release with attached wheel + sdist
  - [x] `git tag -a v0.2.20` + push

## Acknowledgments

Reviewer caught the row-3 overflow and the path-range collision in
one screenshot — *"the third row at the bottom isn't done well, the
border came out, please fix it; then look overall."* — fair point;
fixed plus three more issues caught in the sweep.
