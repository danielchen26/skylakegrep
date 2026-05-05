# Plan — Fundamental homepage redesign (`docs/index.html`)

**Date filed:** 2026-05-05
**Status:** Open · design complete, ready to implement
**Trigger:** User feedback after the 0.2.13 ship:
> 当前 Github 上面的这个主页传达的消息并不够都是堆砌的你能不能够
> fundamentally 的再 improve 一下…把当前的罗列式的东西给别人一看
> 就知道我们这个项目牛逼想去用

---

## Diagnosis — what's wrong with the current page

`docs/index.html` is 1473 lines, 17 H2 / H3 sections all stacked
linearly:

```
hero (90-497, 28% of page) — 4 stat tiles + 6 compare rows + 4 use cases +
                              6-row content-types table + 6-card What's-new
install (498)
quickstart (530)
indexing (549)        ← Concepts deep-dive
ranking (606)         ← Concepts deep-dive
output (645)          ← Concepts deep-dive
architecture (675)    ← Architecture diagram
schema (689)          ← SQLite schema diagram
ranking-pipeline (710)← another diagram
cli (723, 137 lines)  ← full command cheatsheet
configuration (860)   ← env-var table
json-schema (908)     ← JSON output spec
benchmarks (940, 360 lines) — 4 sub-benches + worked example + honest framing
limitations (1296)
roadmap (1349)
```

The five biggest problems:

  1. **No 5-second pitch.** Headline `Find what you mean, not
     what you typed.` is poetic but doesn't say what the project
     does. A first-time visitor doesn't know in 5 seconds whether
     this is for them.
  2. **"What's new" stacks every release card.** 13 release
     cards (0.2.0 - 0.2.13) live in the hero section. Nobody
     visiting the page for the first time cares about your
     version-by-version history. This belongs on a separate
     `/changelog` page.
  3. **Six concepts pages stacked verbatim.** Indexing / Ranking /
     Output / Architecture / Schema / Ranking pipeline. Total
     ~440 lines of deep technical doc. Useful for an engineer
     evaluating the project, fatiguing for a casual visitor.
  4. **Stat tiles abstract.** "Auto-routed", "0 cloud", "Any
     file", "−82 %". Nobody decodes those without already knowing
     the tool.
  5. **Show, don't tell.** No real terminal demo. The hero
     should make the visitor see the tool in action, not read
     about it.

---

## New structure (target: ~600 lines, −60 %)

### Section 1 — Hero (above the fold, ~100 lines)

```
┌─────────────────────────────────────────────────────────────────┐
│  [skylakegrep wordmark]                                          │
│                                                                   │
│  Find anything                                                    │
│  on your machine.                                                 │
│                                                                   │
│  Semantic search for code, PDFs, notes, and docs.                 │
│  Fully offline. No cloud. No telemetry.                           │
│                                                                   │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │  $ skygrep "where does the auth token get refreshed?"     │   │
│  │                                                            │   │
│  │  === auth/middleware.py:78-94 (score: 0.91) ===            │   │
│  │      async def renew_session(req: Request):                │   │
│  │          if req.cookies.get("rt") and access_expired:      │   │
│  │              ...                                            │   │
│  │                                                            │   │
│  │  [0.5s · path=cosine-cheap · σ-gap=0.082 → high-conf]      │   │
│  └───────────────────────────────────────────────────────────┘   │
│                                                                   │
│  [Install in 30s →]   [See benchmarks →]                          │
│                                                                   │
│  30/30 OSS recall · ~1s warm queries · 100% local · v0.2.13       │
└─────────────────────────────────────────────────────────────────┘
```

**Headline:** `Find anything on your machine.` — concrete,
declarative, no metaphor.

**Subhead:** `Semantic search for code, PDFs, notes, and docs.
Fully offline. No cloud. No telemetry.` — three differentiation
axes (semantic, multi-content, privacy) in 13 words.

**Demo box:** real terminal output of one canonical query. No
animation needed for v1; static is fine. v2 can rotate through
3 queries with CSS keyframes.

**Two CTAs:**
  - Primary `Install in 30s →` jumps to `#install`
  - Secondary `See benchmarks →` jumps to `#benchmarks`

**Trust strip:** four facts on one line, separator-delimited.
Concrete, scannable: `30/30 OSS recall · ~1s warm · 100% local
· v0.2.13`. Not abstract.

### Section 2 — Three scenarios (~100 lines)

Replace the current 4-tile stat-grid + 4-card use-case-grid +
8-row content-types table with **three scenario cards**, each
with persona + query + actual terminal output:

```
┌─────────────────────────┐  ┌─────────────────────────┐  ┌─────────────────────────┐
│  CODE BY CONCEPT         │  │  CROSS-CONTENT          │  │  ANY LANGUAGE, PRIVATE   │
│                          │  │                          │  │                          │
│  Find code by what it    │  │  Search code, PDFs, and │  │  Ask in any language.    │
│  does, not what it's     │  │  Word docs in one query.│  │  Files never leave your  │
│  called.                 │  │                          │  │  laptop.                 │
│                          │  │                          │  │                          │
│  $ skygrep "auth         │  │  $ skygrep "the         │  │  $ skygrep "我昨天写的    │
│    refresh"              │  │    proposal I wrote     │  │    auth 代码"             │
│                          │  │    last quarter"        │  │                          │
│  ✓ renew_session() in    │  │  ✓ Q3-proposal.pdf      │  │  ✓ middleware.py:78      │
│    middleware.py:78      │  │    in ~/Downloads       │  │    matches 中英 mixed     │
│                          │  │  ✓ design-doc.md        │  │    intent                │
│                          │  │  ✓ planning.docx        │  │                          │
└─────────────────────────┘  └─────────────────────────┘  └─────────────────────────┘
```

Each card: ~30 lines HTML, terminal-style mini-demo, one
takeaway sentence. The scenarios make the value tangible by
showing real (synthetic) query / result pairs in three different
modes.

### Section 3 — Comparison table (~50 lines)

One simple 4 × 4 table. No padding, no animation, just a clean
grid:

```
                           skylakegrep   ripgrep   cloud RAG (Cursor, ...)
─────────────────────────────────────────────────────────────────────────
Find code by concept           ✓             ✗            ✓
Privacy / no data egress       ✓             ✓            ✗
PDFs · docs · markdown         ✓           text only      ✓
Setup                       pip install   pip install   account + sub
Cost                          $0/mo         $0/mo       $20–100/mo
Vocab mismatch handled         ✓             ✗            ✓
─────────────────────────────────────────────────────────────────────────
```

The current page has a 6-row "Where it pulls ahead" panel
that's already roughly this shape; we'll just simplify it.

### Section 4 — How it works (~60 lines)

Three horizontal boxes showing the pipeline, with one paragraph
introduction:

```
   Query                  LLM router               Cosine cascade
  ┌──────┐              ┌──────────┐              ┌──────────┐
  │ user │   ────►      │qwen2.5:3b│   ────►     │  bge-m3  │   ────►   results
  │ text │              │  intent  │              │ + rerank │
  └──────┘              └──────────┘              └──────────┘
                       (50ms · local)            (0.5–2s · local)

  Local Ollama + SQLite. Zero network calls. Zero subscription.
  Same architecture handles code · PDFs · notes · markdown · any file you index.
```

One paragraph that points at the three differentiators (LLM
understanding, semantic substrate, fully local) without lecturing
the user about cascade tiers, σ-gap, or symbol channels. Power
users will find that detail in the linked Architecture page.

### Section 5 — Get started (~40 lines)

Three copy-pasteable commands:

```
$ pip install skylakegrep
$ ollama pull bge-m3 qwen2.5:3b qwen2.5:1.5b
$ skygrep "your question here"
```

Plus a fourth optional command:

```
$ skygrep setup    # register with Claude Code / Codex / OpenCode / Cursor / Gemini CLI
```

With per-line copy buttons (HTML `data-clipboard` if we want to
go fancy; plain `<pre><code>` if not).

### Section 6 — Public OSS bench (~40 lines)

Replace the four stacked sub-benchmark sections with **one
collapsed-by-default section**:

```
30 / 30 recall on Django + React + Tokio public OSS at 60×–770× less
context than rg's term-OR scan.

[ Reproduce in 4 commands → ]
[ See per-task analysis → ]
[ Read the worked example → ]
[ Read all 4 benchmarks → ]   ← link to existing /benchmarks page
```

The four current sub-benches — end-to-end agent, public OSS,
worked example, self-test — become a **separate page**
(`docs/benchmarks.html`) that the link goes to. The homepage
just states the headline number.

### Section 7 — Trust + footer (~50 lines)

Releases · Source · License · PyPI · Docs · Twitter (if any).

---

## What gets cut / collapsed / moved

| Current section | Action | Where it goes |
| --- | --- | --- |
| `What's new in 0.2.x` (13 cards) | **MOVED** | `docs/changelog.html` (new page); link from footer |
| `Concepts — Indexing` | **MOVED** | `docs/concepts.html` (new page) |
| `Concepts — Ranking` | **MOVED** | `docs/concepts.html` |
| `Concepts — Output` | **MOVED** | `docs/concepts.html` |
| `Architecture` diagram | **MOVED** | `docs/architecture.html` (new page) |
| `Schema` diagram | **MOVED** | `docs/architecture.html` |
| `Ranking pipeline` figure | **MOVED** | `docs/architecture.html` |
| `CLI cheatsheet` (137 lines) | **MOVED** | `docs/cli.html` (mirror of README) |
| `Configuration` env-var table | **MOVED** | `docs/cli.html` or `docs/reference.html` |
| `JSON schema` | **MOVED** | `docs/reference.html` |
| `4 sub-benchmarks` | **MOVED** | `docs/benchmarks.html` |
| `Honest framing` | **MOVED** | `docs/benchmarks.html` |
| `Limitations` | **KEPT but condensed** | inline section, ~10 lines |
| `Roadmap` | **MOVED** | `docs/roadmap.md` (already exists) |

The current 1473 lines split:
  - Homepage `index.html`: ~600 lines (the seven sections above)
  - New `changelog.html`: ~150 lines (auto-generated from
    `docs/skylakegrep-X.Y.Z.md`)
  - New `concepts.html`: ~200 lines (Indexing + Ranking + Output)
  - New `architecture.html`: ~150 lines (Architecture + Schema +
    Ranking pipeline)
  - New `cli.html`: ~200 lines (CLI cheatsheet + Configuration)
  - New `reference.html`: ~80 lines (JSON schema + misc)
  - New `benchmarks.html`: ~360 lines (the 4 sub-benches +
    honest framing — the entire current `#benchmarks` section)

Total across all pages: ~1740 lines. We're not deleting content,
we're **reorganising it so the homepage doesn't have to scroll
1473 lines before the user learns whether the tool is for them**.

---

## CSS classes — reuse, don't rewrite

The existing `docs/styles.css` already has:

  - `.hero`, `.lead`, `.eyebrow`, `.button-row`, `.button`,
    `.button.primary`, `.button.secondary`
  - `.compare-grid`, `.compare-row`, `.compare-task`,
    `.compare-tool`, `.compare-skygrep`, `.compare-check`
  - `.usecase-grid`, `.usecase-card`, `.usecase-tag`,
    `.usecase-title`, `.usecase-body`
  - `.stat-grid`, `.stat-tile`, `.stat-num`, `.stat-label`,
    `.stat-sub`
  - `.table-wrap`, `.doc-section`

The redesign **reuses all of these**. We don't add new CSS
classes; we re-purpose what already exists.

  - 3 scenario cards → reuse `.usecase-card` with terminal-style
    inline body
  - Comparison → reuse `.compare-grid` (already 6 rows; trim to 6
    most-impactful)
  - How it works → use a new minimal flex layout with inline
    styles (no new class)
  - Get started → reuse `<pre><code>` already styled
  - Trust strip → use `.eyebrow` styling for the one-line bar

The terminal-demo box in the hero needs **one small new bit of
CSS** (about 20 lines) to render the syntax-highlighted style.
We'll inline it in `<style>` to avoid touching `styles.css`.

---

## Implementation phases

### Phase H-1 — restructure homepage (~3 h)

  1. Save current `docs/index.html` as `docs/index.html.bak.0213`
     for diffing.
  2. Strip the current page down to: hero + scenarios +
     comparison + how-it-works + get-started + bench-headline +
     limitations + footer.
  3. Move the deleted sections into the 5 new pages
     (`changelog.html`, `concepts.html`, `architecture.html`,
     `cli.html`, `reference.html`, `benchmarks.html`).
  4. Add navigation links in the topbar to the 5 new pages.
  5. End-to-end visual check: open `docs/index.html` in browser,
     verify it loads / scrolls / clicks correctly.

### Phase H-2 — populate new pages (~2 h)

  1. `changelog.html`: lift each `docs/skylakegrep-X.Y.Z.md` body
     into a section.
  2. `concepts.html`: lift Indexing + Ranking + Output sections.
  3. `architecture.html`: lift Architecture + Schema + Ranking
     pipeline.
  4. `cli.html`: lift CLI cheatsheet + Configuration.
  5. `reference.html`: lift JSON schema.
  6. `benchmarks.html`: lift the 4 sub-bench sections + honest
     framing.

### Phase H-3 — write the actual scenario copy + demo box (~1 h)

The 3 scenario cards need real synthetic queries / results that
demonstrate the value. Drafts are in this plan; the
implementation needs to render them inside `.usecase-card`
with terminal styling.

The hero demo box needs one fixed query + result. We'll use the
canonical "where does the auth token get refreshed?" → middleware.py:78
example, since the README and 0.2.x release notes already use it.

### Phase H-4 — visual polish + accessibility (~30 min)

  - Mobile breakpoints — hero collapses to single column.
  - Test all anchor links still work.
  - Run a contrast check on the trust strip.
  - Validate HTML.

### Phase H-5 — release as 0.2.14 (homepage redesign release) (~30 min)

  - Bump version 0.2.13 → 0.2.14.
  - Write `docs/skylakegrep-0.2.14.md` release notes (focused on
    the homepage / docs reorganisation).
  - Update `RELEASING.md` to add the new homepage pages to the
    8-surface checklist (so future releases don't drift again).
  - Standard release pipeline: commit · push · build · tag ·
    twine · `gh release create`.

Total: ~7 hours, end-to-end.

---

## Risks

  - **Information loss.** Moving content to subpages means people
    deep-linking to the homepage's old anchor IDs (e.g.
    `#benchmarks`, `#cli`) will hit a 404 unless we redirect.
    Mitigation: keep the H2 anchor IDs on the new pages (so
    `/benchmarks.html#bench-multilang` still works), and add
    `<meta http-equiv="refresh">` redirects on the homepage for
    the old anchors → new pages.
  - **SEO regression.** The homepage currently has a lot of
    crawlable content. Moving 60 % of it to subpages means the
    crawler index updates take a few weeks. Probably fine for a
    project at this scale; not really a risk.
  - **Visual review needs eyes.** This is one of those things
    where unit tests can't catch ugliness. End-to-end means open
    the new HTML in a real browser, take screenshots, iterate.
    The user (the project author) is the visual reviewer.
  - **CSS conflicts when porting old sections to new pages.**
    Some `.doc-section`-styled content was scoped to be inside
    the topbar+footer chrome of `index.html`; the subpages will
    need the same chrome wrapper or a `subpage.html` template.

---

## Measurement plan (post-ship)

A homepage redesign isn't just "did it ship" — it's "did it
work". Two simple metrics:

  1. **Bounce rate on the homepage.** GitHub Pages has visitor
     analytics if you opt in. Compare 2 weeks pre-redesign to 2
     weeks post-redesign.
  2. **Click-through to install / quickstart.** GitHub Pages
     anonymous click counts on the primary CTA.

If bounce rate drops or CTA click-through rises, the redesign
worked. If neither moves, we re-evaluate the headline.

---

## Open questions (to resolve before Phase H-1)

  1. **Animated terminal vs static demo?** Static is faster /
     simpler / accessible. Animated (3-query rotation) is more
     "show, don't tell" but adds JS / CSS keyframes. **Default:
     static for v1, revisit later.**

  2. **Comparison row order?** The 6 current rows are mostly
     concrete features. Should we lead with the privacy story
     (`local-only` is the strongest differentiation) or the
     semantic-search story (`vocab mismatch handled`)? **Default:
     lead with vocab mismatch — it's the *behaviour* differentiator;
     privacy is the *trust* differentiator and lives below.**

  3. **Scenario copy realism.** The 3 scenarios in this plan are
     synthetic. We want them to feel real without exposing user-
     personal data (per the 0.2.13 sanitisation). The drafts use
     `where does auth refresh` / `the proposal I wrote last
     quarter` / `我昨天写的 auth 代码` — none of which leak the
     actual project author's data. **Verify before ship.**

  4. **Topbar nav shape.** With 5 subpages, a horizontal topbar
     with 5 links + the CTA gets crowded. Options: hamburger,
     dropdown, or a simpler 3-link nav (Get Started · Benchmarks
     · Architecture) with the rest behind a "More ▾" dropdown.
     **Default: 3 visible + "More ▾" — same shape Stripe and
     Vercel use.**

---

## What this plan does NOT change

  - `README.md` — the README is the primary surface for users
    arriving from PyPI / GitHub. The README's current What's-new
    table + capability matrix + cheatsheet are appropriate there
    and stay unchanged. The homepage (`docs/index.html`) is for
    visitors arriving via GitHub Pages — different audience,
    different pacing.
  - `docs/PRINCIPLES.md` — unchanged.
  - `docs/RELEASING.md` — gets one update to add the 5 new pages
    to the 8-surface checklist.
  - `docs/skylakegrep-X.Y.Z.md` release notes — unchanged.
  - Source code — unchanged.

---

## Decision

**Filed in `docs/plans/` per the user's instruction.** The
implementation is self-contained (frontend only, no Python code
change, no test impact). Ready to execute when the user
confirms.

The structure here is the *target*; iterations on copy /
visuals will happen during Phase H-3 / H-4 with the user as the
visual reviewer.

---

## Cross-references

  - [`docs/RELEASING.md`](../RELEASING.md) — the existing
    8-surface release checklist needs the 5 new pages added.
  - [`docs/PRINCIPLES.md`](../PRINCIPLES.md) Principle 4 — public
    surfaces sync at every release. Adding 5 new pages multiplies
    the surface count, which is a real cost; the redesign is
    only worth it if the homepage clarity improvement is real.
  - Other plan files in this folder
    (`2026-05-05-graph-prior-folder-inference.md`,
    `2026-05-05-conversational-session-state.md`,
    `2026-05-05-phase-c-audit.md`,
    `2026-05-05-phase-c-exploration.md`) are orthogonal — none
    block this redesign, and this redesign doesn't block them.
