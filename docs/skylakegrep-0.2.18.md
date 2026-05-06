# skylakegrep 0.2.18 — homepage layout fix · full-width content

This is a **documentation-surface release**, not a code-path release.
Zero behaviour change, byte-compatible 0.2.17 indexes, same wheel
surface, same CLI, same JSON contract.

## What changed

The 0.2.17 GitHub Pages homepage had a layout regression: on wide
screens (1500px+), the comparison-matrix card was being clipped on
the right side — the **Sourcegraph Cody** column got cut off — while
~450px of empty space sat unused on the right side of the viewport.

Root cause: `docs/index.html` was still using the legacy 3-column
docs-shell grid (`sidebar / content / TOC`) with `--content-max:
800px` capping the middle column. The right TOC was still rendered
even though the new homepage didn't need anchor navigation, and the
800px cap was too narrow for a 5-column comparison grid.

Fixed in this release:

  - Removed the right `<aside class="toc">` element from
    `docs/index.html` — the new short-section homepage doesn't need
    it (the sidebar already covers global navigation).
  - Inline-CSS override for the homepage:
    ```css
    .docs-shell {
      grid-template-columns: var(--sidebar-w) minmax(0, 1fr);
      max-width: 1500px;
    }
    .content { width: 100%; max-width: 100%; }
    .themed-card, .why-card { max-width: 100%; }
    ```
  - The comparison card now uses the full ~1200px of available
    horizontal space; the Cody column renders complete, no clipping;
    the right side has no wasted whitespace.
  - Subpages (`concepts.html`, `architecture.html`, `cli.html`,
    `reference.html`, `benchmarks.html`, `changelog.html`) are
    unaffected — they keep their own subpage chrome.

## Compatibility

  - Python: unchanged — 3.9+
  - Default embedder / LLM router: unchanged — `bge-m3` / `qwen2.5:3b`
  - Wheel surface: unchanged
  - Index format: byte-compatible with 0.2.17
  - JSON output schema: unchanged

## Bench numbers

Unchanged from 0.2.17. 30 / 30 public-OSS recall holds.

## Eight-surface checklist

  - [x] `pyproject.toml` 0.2.17 → 0.2.18
  - [x] `docs/skylakegrep-0.2.18.md` (this file)
  - [x] `README.md` v0.2.18 in pill text
  - [x] `docs/index.html` — right TOC removed; layout overrides for
        full-width content; v0.2.18 in why-grid header
  - [x] `docs/changelog.html` — 0.2.18 release card
  - [x] `docs/assets/*.svg` v0.2.17 → v0.2.18 in version pills
  - [x] `docs/assets/og-image.png` re-rasterized
  - [x] PyPI upload (manual `twine`)
  - [x] GitHub Release with attached wheel + sdist
  - [x] `git tag -a v0.2.18` + push

## Acknowledgments

Reviewer caught it on first scroll: *"Why is it cut off, and why is
there a lot of empty area on the right? You can show the content
horizontally — no whitespace."* — fair point, fixed.
