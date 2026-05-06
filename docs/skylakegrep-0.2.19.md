# skylakegrep 0.2.19 — every clickable link now stays in the themed shell

This is a **documentation-surface release**, not a code-path release.
Zero behaviour change, byte-compatible 0.2.18 indexes, same wheel
surface, same CLI, same JSON contract.

## What changed

The 0.2.18 GitHub Pages site had 25+ clickable links that bounced
out of the themed shell into raw markdown — every "Full notes →" on
the changelog page jumped to a `github.com/.../blob/master/docs/
skylakegrep-X.Y.Z.md` URL, which renders the markdown using GitHub's
generic chrome instead of the skylakegrep frosted-glass aesthetic.

Same problem for the **Principles** link in every subpage's sidebar,
the **parity-benchmarks** link on the homepage, the **token-
benchmarking** link on the benchmarks page, and the
**skylakegrep-0.1.0.md** references inside `benchmarks.html`. Visitors
clicked once, dropped out of the themed experience, and were now on
github.com.

This release fixes that comprehensively.

### 25 markdown documents rendered as themed HTML

Every clickable `.md` reference now has a matching themed `.html`
page using the **same shared chrome** as the existing subpages
(topbar + sidebar + slate-blue glass shell):

  - **20 release notes**: `skylakegrep-0.1.0.html` through
    `skylakegrep-0.2.19.html`. Each gets the full sidebar nav, the
    skylakegrep brand mark, the lifted slate-blue background.
  - **`principles.html`** — six architectural principles (was
    `PRINCIPLES.md`).
  - **`parity-benchmarks.html`** — full bench protocol (was
    `parity-benchmarks.md`).
  - **`token-benchmarking.html`** — token-reduction methodology
    (was `token-benchmarking.md`).
  - **`roadmap.html`** — release roadmap (was `roadmap.md`).
  - **`releasing.html`** — eight-surface release checklist (was
    `RELEASING.md`).

Markdown rendering uses Python `markdown` with `extra`, `tables`,
`fenced_code`, `sane_lists` extensions. Each rendered body sits
inside a `.md-body` article styled to match the homepage typography
(slate-blue panel backgrounds for code, frosted-glass blockquote
accents, dashed-underline accent links, monospace code blocks with
the dark-glass aesthetic).

### 83 link rewrites across 33 files

Every reference that used to go to a raw markdown URL now points
at the themed sibling page:

  - **13 "Full notes →" links** in `changelog.html` — each release
    card now opens the themed release-notes page.
  - **6 "Principles" links** in subpage sidebars (every subpage
    Project nav).
  - **4 `parity-benchmarks` links** in `benchmarks.html` and
    `index.html`.
  - **1 `token-benchmarking` link** in `benchmarks.html`.
  - **2 `skylakegrep-0.1.0.md` references** inside
    `benchmarks.html` (cited from the v0.1.0 release context).
  - **9 `.md` references in `README.md`** also rewritten to point
    to the themed `docs/<name>.html` paths so clicks from the
    GitHub README into the docs site stay in the theme.

Visitors can now traverse the entire site — release history,
principles, bench protocol, sub-bench reproductions, roadmap — and
**never leave the slate-blue frosted-glass shell**.

## Compatibility

  - Python: unchanged — 3.9+
  - Default embedder / LLM router: unchanged — `bge-m3` /
    `qwen2.5:3b`
  - Wheel surface: unchanged
  - Index format: byte-compatible with 0.2.18
  - JSON output schema: unchanged

## Bench numbers

Unchanged from 0.2.18. 30 / 30 public-OSS recall holds.

## Eight-surface checklist

  - [x] `pyproject.toml` 0.2.18 → 0.2.19
  - [x] `docs/skylakegrep-0.2.19.md` (this file) + rendered
        `skylakegrep-0.2.19.html`
  - [x] `README.md` v0.2.19 in pill text + 9 markdown links
        rewritten to themed HTML
  - [x] `docs/index.html` v0.2.19 + 2 link rewrites
  - [x] `docs/changelog.html` — 13 "Full notes →" links rewritten;
        0.2.19 release card added
  - [x] `docs/concepts.html` + `architecture.html` + `cli.html` +
        `reference.html` + `benchmarks.html` — Principles link
        rewritten
  - [x] **20 new themed `skylakegrep-X.Y.Z.html` pages** (one per
        historical release, 0.1.0 → 0.2.19)
  - [x] **5 new themed reference pages**: `principles.html`,
        `parity-benchmarks.html`, `token-benchmarking.html`,
        `roadmap.html`, `releasing.html`
  - [x] All 6 SVG version pills bumped 0.2.18 → 0.2.19;
        `og-image.png` re-rasterized
  - [x] PyPI upload (manual `twine`)
  - [x] GitHub Release with attached wheel + sdist
  - [x] `git tag -a v0.2.19` + push

## Acknowledgments

Reviewer feedback in one sharp sentence: *"Anything clickable
shouldn't drop me into a raw markdown file — keep the same theme
on every page."* — fair point; **25 markdown documents → 25
themed HTML pages**, 83 link rewrites, all in one release.
