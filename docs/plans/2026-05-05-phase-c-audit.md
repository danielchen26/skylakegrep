# Phase C audit — content-agnostic, language-agnostic, general intelligence?

**Date:** 2026-05-05
**Context:** After shipping 0.2.0 (substrate upgrade) + 0.2.1 (positioning
correction), the user asked whether the proposed Phase C plan
(symbol-channel auto-router) actually matches the content-agnostic
philosophy the project committed to.

This document captures the honest review. Open question; pick a
path before starting Phase C work.

---

## The four standards the user asked about

| Standard | Phase C as I designed it | Honest verdict |
| --- | --- | --- |
| **Language-agnostic** | tree-sitter symbols only — Rust / Python / JS / TS today; Go / Ruby / Java / Swift would each need a new grammar + extractor branch | ✗ **enumerated, not principled** |
| **Content-agnostic** | symbol-channel only fires on code; markdown / PDF / YAML / knowledge-graph corpora silently no-op | ✗ **code-only intelligence on top of a content-agnostic substrate** |
| **General intelligence / general routing** | router signals (query morphology + σ_topK) **are** content-agnostic; but they route to a code-only channel | ⚠ **half-half — general router, specific channel** |
| **Maintains / improves latency** | router adds ~1 ms regex + reuses the σ_topK already computed; only the FUSE branch pays a +50–200 ms SQL hit | ✓ **expected neutral; current 0.2.1 is already −19 % vs 0.1.0** |

**Bottom line:** Phase C as currently sketched is **not** truly
general intelligent routing. It is "general router → code-specific
channel". This conflicts with the 0.2.0 / 0.2.1 content-agnostic
ship narrative.

---

## Latency context (baseline for any future change)

Three-repo public-OSS bench, per-query latency:

| | 0.1.0 (mxbai) | 0.2.x (bge-m3) | Δ |
| --- | :-: | :-: | :-: |
| Tokio | 14 s | 21.9 s | **+57 %** |
| Django | 20 s | 10.1 s | **−50 %** |
| React | 20 s | 11.7 s | **−42 %** |
| **Aggregate avg** | **~17 s** | **~14.6 s** | **−19 %** |

So `0.2.x` is faster on the three-repo average, slower on Tokio.
Any Phase C variant must hold this or improve it.

---

## Three candidate paths forward

### A. Phase C as originally designed (code-specific symbol channel)

- **What:** Symbol-channel as opt-in retrieval channel + auto-router
  (`SKYGREP_SYMBOL_CHANNEL=auto|on|off`)
- **Effort:** 8–10 h
- **Risk:** Medium (known failure modes — react-010 fusion regression)
- **Pros:** Concrete deliverable; symbol queries (`find useState impl`,
  `where is tokio::spawn defined`) get a 0.3.0 feature
- **Cons:** Violates content-agnostic principle; markdown / PDF /
  knowledge-graph users get nothing
- **Suitable when:** Real use case is overwhelmingly code search;
  docs / PDFs are second-class

### B. Phase C-General (content-agnostic structural channel registry)

- **What:** Generalise `symbol_channel.py` → `structural_channel.py`
  with a `register_structural_extractor(name, extensions, fn)`
  registry. Built-in extractors:
  - code → tree-sitter symbols (already have)
  - markdown → headings + wiki-link targets
  - PDF → section titles
  - YAML → top-level keys
  - (extensible)
- Router signal generalises: `looks_like_structural_ref(query)`
  detects camelCase OR `# Heading` OR `Section 3.2` OR `key:`
- **Effort:** 15–20 h (≈ 2× A)
- **Risk:** Medium-high (more unknown unknowns; bench needs
  expansion for markdown / PDF too)
- **Pros:** **Truly aligned with Karpathy "knowledge graph as
  prior" / 0.2.x content-agnostic philosophy.** Markdown users get
  heading-channel intelligence; PDF users get section-channel; new
  content types plug in via one line
- **Cons:** Effort doubles; markdown / PDF benches don't exist yet
- **Suitable when:** The content-agnostic flag must be vindicated
  end-to-end, not just at the substrate layer

### C. Smarter cascade, no new channel

- **What:** Don't introduce a new channel. Instead, sharpen the
  existing cascade with a query-conditional decider:
  - When σ_topK is high AND query is intent-style → skip HyDE
    rewrite **and** skip cross-encoder rerank (faster cosine-only)
  - When σ_topK is low OR query is symbol-shaped → escalate to
    rerank (current default behaviour)
  - When σ_topK is medium → run rerank but skip HyDE
- **Effort:** 4–6 h
- **Risk:** Low (no new dependencies; cascade-internal only)
- **Pros:** Zero new dependencies; fully content-agnostic; lowest
  risk; doubles down on the σ-adaptive principle already shipped
- **Cons:** Measurable improvement may be small — the current
  cascade is already near-optimal on bge-m3
- **Suitable when:** "Adding a new channel" is itself the
  over-engineering symptom; intelligence should be smarter
  scheduling of existing parts

### Comparison

| | A (code symbol) | B (structural registry) | C (smarter cascade) |
| --- | :-: | :-: | :-: |
| Content-agnostic | ✗ | ✓ | ✓ |
| Language-agnostic | ✗ | ⚠ (per content-type) | ✓ |
| General router | ✓ | ✓ | ✓ |
| Latency-safe | ✓ | ✓ (router only fires when applicable) | ✓ (only saves time, never adds) |
| Effort (h) | 8–10 | 15–20 | 4–6 |
| Risk | medium | med-high | low |
| Bench coverage exists | partial (need to add) | mostly absent (need code + markdown + PDF benches) | yes (existing 30) |

---

## My ranking (when revisiting)

| Rank | Path | Why |
| --- | --- | --- |
| **★ 1** | **C** (smarter cascade) | Most aligned with 0.2.1 philosophy; no new deps; lowest risk; but possible upper bound on visible improvement |
| 2 | **B** (structural registry) | Vindicates content-agnostic flag; high effort but principled |
| 3 | **A** (code-specific) | Quickest concrete deliverable but conflicts with the content-agnostic story |

---

## The original 5-step plan (for record)

The plan described in conversation before this audit:

1. **Expand bench** with symbol-heavy queries per repo (5–10 each)
2. **Baseline** — run cosine-only on the new bench
3. **Implement router** (`SKYGREP_SYMBOL_CHANNEL=auto|on|off` +
   `should_fuse = looks_like_symbol(query) OR sigma_topK < threshold`)
4. **Double-bench validation** — old 30/30 must hold; new symbol bench
   must improve
5. **Ship-or-cut** — both pass → 0.3.0; either fails → delete
   `symbol_channel.py`

This plan applies cleanly to **path A**. Path B requires expanded
benches (code + markdown + PDF) and a registry refactor before
step 3. Path C replaces step 3 with cascade-internal scheduler
work and uses the existing 30-query bench as-is.

### Honest concerns about the 5-step plan

| Concern | Mitigation |
| --- | --- |
| Step 1 author-bias when writing symbol benches | Mix three categories per task: pure-symbol (channel should win), pure-NL (channel should not win), mixed (router should choose). The pure-NL queries are the falsifiability guard. |
| Step 4 latency leak (router adds 1 ms × N queries) | Track per-repo latency in addition to hit rate; 30/30 with > +10 % latency is a fail. |
| Step 5 deletion temptation | Pre-commit a numeric threshold for the cut decision before starting (e.g., "any old-bench miss OR new-bench delta < +3 — delete"). |
| n=60 sample is measurement-grade, not study-grade | Already documented under Limitations. Acceptable for project scale; do not overclaim. |

---

## What's actually shipped today (0.2.1)

- bge-m3 substrate (multilingual, symmetric, 1024-d, 8k context)
- Content-agnostic reference-graph registry + markdown extractor
- σ-adaptive cascade gap (`max(τ_floor, k·σ_topK)`)
- Universal non-canonical-path filter (24 patterns)
- Symbol channel — internal, opt-in, **not in default link**
- Comprehensive content-agnostic positioning across pyproject + GitHub
  repo description + README + GitHub Pages site
- 30 / 30 public OSS bench with a worked-example deep-dive on
  `django-001` (≈ 1,395× token reduction on a vocab-mismatch query)

This file is for revisiting Phase C when the user is ready to pick
a path. The 0.2.1 baseline is stable; nothing here blocks shipping
follow-ups in another shape (e.g., re-rendering SVG assets,
re-running self-test bench on bge-m3, fixing the GitHub Actions
`PYPI_API_TOKEN` 403, etc.).
