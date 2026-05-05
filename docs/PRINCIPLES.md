# Project principles for skylakegrep

This document is the **deepest memory** of the project — durable
guidance for any contributor (human or AI agent) working in this
repository. Loaded into Claude sessions via `CLAUDE.md` so the
principles travel with the code.

---

## Principle 1 — Understanding > Enumeration

**The anti-pattern:** when a question can be answered by a generic
*understanding* layer (a language model, a multilingual embedder,
a pluggable registry), do not answer it by enumerating cases
(per-language regex, per-keyword token list, per-content-type
hardcoded branch). Enumeration is a patch you can never finish; new
vocabulary, new languages, new content types appear faster than you
can add them.

**The pattern:** identify the substrate or registry that already
handles the generic case. Use it as the *primary* path. Keep
enumeration only as an **offline fallback** when the substrate is
unavailable, with a written rationale for why the enumeration is
acceptable in that bounded context.

### Past lapses in this project (the receipts)

These are real mistakes the project has made — recorded here so
future contributors see the pattern and don't repeat it.

| Lapse | Anti-pattern instance | Principled fix | Released in |
| --- | --- | --- | --- |
| **code_graph.py** | hardcoded Rust + Python + JS + TS regex extractors as the sole way to build the file-export graph; new languages required new regex branches inside the retrieval module | `reference_graph.register_extractor(name, extensions, fn)` — the abstraction is now "A references B"; `code_graph.py` is a 75-line back-compat facade; new content types (markdown shipped, YAML / knowledge-graph / Obsidian one line away) plug in without touching retrieval | `0.2.0` |
| **mxbai-embed-large substrate** | English-and-code-only embedder ranked re-export aggregators above canonical implementations; Chinese / mixed-language code comments performed poorly | `bge-m3` substrate (multilingual XLM-RoBERTa, symmetric, 8 k context); query and passage share the same vector space; new languages cost zero code | `0.2.0` |
| **symbol_channel.py** | tree-sitter symbol extraction only knows Rust / Python / JS / TS; adding Go / Ruby / Java requires installing new grammars + extending `symbol_kinds_for_language()`; markdown / PDF / YAML get nothing | tracked as **Phase C path B** (`docs/plans/2026-05-05-phase-c-audit.md`): generalise to a `register_structural_extractor` registry covering code symbols, markdown headings, PDF sections, YAML keys, etc.; the router's `looks_like_structural_ref(query)` becomes content-type-agnostic | open (slated for `0.3.x`) |
| **intelligent_cli._METADATA_TOKENS** | hand-curated set of recency / size / listing keywords (`recent`, `latest`, `最近`, `最新`, …) used to detect out-of-scope queries; the user reported `我昨天打开过的十个文件` → not flagged because `昨天` was missing; patched in `0.2.5` by adding `昨天 / 今天 / 前天 / 上周 / 本周 / 打开过 / 改过 / yesterday / today / this week / last week`; this is a patch, not the answer | `0.2.6`: `RouterDecision` gained an `out_of_scope` field (`none` / `recency` / `size` / `listing`); the existing `llm_router.route_query()` LLM prompt now classifies scope on the same call that's already running for retrieval intent — zero added latency. `intelligent_cli.detect_out_of_scope` consults `decision.out_of_scope` first; the keyword list is now strictly an **offline safety net** for when Ollama is unreachable | `0.2.6` ✓ shipped |

### The rule (for every PR)

Before adding a token to a list, a regex to a language branch, or a
new `if content_type == "x"` arm, the PR description must answer:

  1. Is there a substrate (embedder, LLM router, registry) that
     could handle this generically?
  2. If yes — why isn't it being used? What's blocking?
  3. If no — would a registry / plugin layer make sense here?

Acceptable enumerations:

  - **Offline fallback** for substrate failure (LLM unreachable,
    deterministic CI).
  - **Genuinely closed sets** (Click subcommand names, SQLite type
    affinities, HTTP method verbs).
  - **Plugin defaults** behind a registry where extension is one
    line of caller code.

If none of those apply, the enumeration is wrong. Push back on it.

---

## Principle 2 — Substrate before scaffolding

When accuracy / capability is bounded by the underlying substrate
(embedder, model, vector space, parser), no amount of clever
re-ranking, prior weighting, graph traversal, or rule-based
filtering can break the ceiling. Upgrade the substrate first; layer
priors on top only when the substrate is good enough that the
priors have signal to work with.

**Example:** Phase 1 of the bge-m3 work tried a parade of priors
(P4-LFA / P4-CGC / P4-MH / RRF rerank / multi-channel fusion) on
the `mxbai-embed-large` substrate and **all of them returned null
or regressed**. Switching the substrate to `bge-m3` alone broke the
28/30 → 30/30 ceiling that no prior could touch. The priors came
back into play once the substrate was strong enough that they had
signal to refine.

---

## Principle 3 — Latency / quality / correctness, in that order

When trade-offs collide, the priorities are:

  1. **Correctness** — never silently return wrong results. If we
     can't answer well (vocab mismatch, out-of-scope query, broken
     index), say so up front via an `intelligent_cli` hint and let
     the user redirect.
  2. **Quality** — semantic-quality answers beat rg-quality
     answers; full re-embed beats stale-dim filtered.
  3. **Latency** — prefer instant rg-fallback under degradation
     over blocking the user; pay LLM router cost once amortized
     across the full search.

Background workers (recovery, watch, serve) exist to give the user
**both** a fast first answer AND eventual full quality.

---

## Principle 4 — Public surfaces must be sync'd at every release

`docs/RELEASING.md` codifies the eight surfaces every release
touches. The 0.2.2 → 0.2.3 lapse (PyPI shipped, GitHub Pages
silent) is the receipts; the checklist is the prevention. No
release is done until every surface is updated.

---

## Principle 5 — Honest evaluation over hopeful claims

Numbers in headlines must be measurable, reproducible, and named
by their bench. Three benches live in this project:

  - **End-to-end Claude Code agent** — tool-call reductions
    (−37.6 % single-turn, −82 % multi-turn).
  - **Public OSS recall** — Django + React + Tokio,
    30 / 30 (100 %) at top-10.
  - **Self-test regression** — 30 internal tasks, recall × token
    reduction across top-k.

Don't combine numbers across benches. When a feature can't be
measured (e.g. "future Phase C wins"), say so — don't claim it.

---

## How this document gets used

  - `CLAUDE.md` imports this file via `@docs/PRINCIPLES.md` so any
    Claude session in this repo loads the principles automatically.
  - Human contributors should read this once on first PR; the rule
    in **Principle 1** is the most likely tripwire.
  - Update this file whenever a new architectural lapse is
    identified — receipts go in the table above; the rule and
    pattern stay stable.
