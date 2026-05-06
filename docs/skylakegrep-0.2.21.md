# skylakegrep 0.2.21 — comparison row label trim · perf banner relayout

This is a **documentation-surface release**, not a code-path release.
Zero behaviour change, byte-compatible 0.2.20 indexes, same wheel
surface, same CLI, same JSON contract.

## What changed

Two more overlap defects caught on the live site:

### 1. Comparison-matrix row 3 — capability label ran into the data column

The row-3 capability label was `Content — code · md · PDF · docx ·
images` (~42 monospace chars). At font-size 13, that occupies
roughly 330 px from `x=20`, ending at `x ≈ 350`. The first data
column is centered at `x=376`, but its 22-character payload
`code · md · PDF · docx` (centered) starts at ~`x=276` — so the
label and the data text overlapped from `x=276` to `x=350`.

Fixed: capability label trimmed from
`Content — code · md · PDF · docx · images` →
**`Content — multimodal`**. The data cells already enumerate the
formats per tool, so the header doesn't need to repeat them. The
new header occupies ~150 px, leaving ~125 px of clean gap before
the first data column.

### 2. Performance-matrix aggregate banner — three big numbers + labels overflowed the card

The banner was a single inline row mixing 32 px green/cyan numbers
and 20 px gray labels: `30 / 30  recall · 60 × – 770 ×  less
context vs rg · −82 %  tool calls (best multi-turn)`. Total content
width ~1163 px, banner only 1032 px wide — `−82 %` collided with
the next label, and the trailing parenthetical bled past the right
edge.

Rebuilt as a **three-column layout** with the headline number on
top and the descriptor below, each column centered:

```
   30 / 30           60 × – 770 ×              −82 %
    recall         less context vs rg     tool calls (best multi-turn)
```

Banner height 100 → 120 px to host the new two-row content cleanly.
The eye now compares the three headline numbers in parallel — the
old jammed-line layout buried the comparison.

## Compatibility

  - Python: unchanged — 3.9+
  - Default embedder / LLM router: unchanged
  - Wheel surface: unchanged
  - Index format: byte-compatible with 0.2.20
  - JSON output schema: unchanged

## Bench numbers

Unchanged. 30 / 30 public-OSS recall holds.

## Eight-surface checklist

  - [x] `pyproject.toml` 0.2.20 → 0.2.21
  - [x] `docs/skylakegrep-0.2.21.md` (this file) + rendered
        `skylakegrep-0.2.21.html`
  - [x] `README.md` v0.2.21
  - [x] `docs/index.html` v0.2.21
  - [x] `docs/changelog.html` — 0.2.21 release card
  - [x] `docs/assets/comparison-matrix.svg` — row-3 label trimmed,
        v0.2.21
  - [x] `docs/assets/performance-matrix.svg` — aggregate banner
        relayout (3 columns), banner height 100 → 120 px, v0.2.21
  - [x] All 6 SVG version pills bumped 0.2.20 → 0.2.21
  - [x] `docs/assets/og-image.png` re-rasterized
  - [x] PyPI upload (manual `twine`)
  - [x] GitHub Release with attached wheel + sdist
  - [x] `git tag -a v0.2.21` + push

## Acknowledgments

Reviewer caught both overlaps in two side-by-side screenshots —
*"this still has overlap, please fix"* — fair point; both fixed in
the same release.
