# Project principles for skylakegrep

This document is the **deepest memory** of the project — durable
guidance for any contributor (human or AI agent) working in this
repository. Loaded into Claude sessions via `CLAUDE.md` so the
principles travel with the code.

---

## Principle 0 — Release Privacy Redline

This is the deepest project rule and it applies before every repo
change, test fixture, benchmark receipt, doc page, release note, GitHub
Release body, PyPI long description, screenshot, and generated artifact:
never publish a user's real prompts, private filenames, private folder
names, local machine paths, document categories, names, email addresses,
or any other information derived from the user's local computer or
conversation.

If a bug report or terminal transcript contains private material, the
first step is to translate it into a fictional placeholder before it is
written to any tracked file. Use generic examples such as `case42`,
`project-report.pdf`, `/Users/example/...`, `~/example-folder`, or
`<filename-A>`. Do not preserve the user's actual wording "for realism";
realism loses to privacy every time.

Every release must run the privacy gate before build and again against
the built wheel/sdist plus public surfaces. Use
`scripts/privacy_release_scan.py`, adding any private terms from the
current conversation or local screenshots via the untracked
`.release-private-patterns` file or `SKYGREP_PRIVATE_PATTERNS`. A release
is blocked until the scan is clean. If private material ever reaches a
public surface, delete or yank that surface first, force-push sanitized
GitHub content if needed, then ship a sanitized patch release.

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

**Heartline:** never fix a user-reported query by adding a special
trigger for that exact wording, language wrapper, private example, or
one-off filename shape. If a query exposes a routing miss, improve the
generic intent substrate or retrieval contract so the whole class of
queries improves. Tests may keep sanitized regression receipts, but the
production path must not become a pile of per-case triggers.

### Adaptive query-plan contract

A user query is not a single label. Treat it as an open-world query
plan made of independent facets that can coexist:

  - **target** — the artifact, symbol, document, or concept being
    searched for.
  - **scope** — where the user wants the search bounded, such as a
    project, repository, folder, or workspace.
  - **metadata** — modifiers such as created, modified, opened, size,
    or order. Metadata can be terminal only when the user is asking for
    a metadata list; otherwise it ranks or filters evidence.
  - **answer depth** — path, preview, source excerpt, explanation,
    structured JSON, or synthesized answer.
  - **retrieval needs** — filename, lexical, semantic, structural,
    graph, or cross-folder expansion. Multiple needs may be active in
    the same query.

The fast model or router may propose these facets, but it is not an
oracle. Deterministic evidence must validate the plan before it is used
to stop work: a scope facet must resolve to a real bounded root; a
filename result must contain concrete basename/path evidence; a
metadata facet must be treated as a modifier unless the query's answer
depth is metadata-only; semantic or synthesized-answer requests must
keep semantic retrieval alive even when a filename anchor is found.

Uncertainty must degrade by broadening carefully, not by scanning the
world. Prefer bounded roots, visible user/project directories, and
hidden/cache/tool-directory suppression over home-wide sweeps. If the
foreground answer is incomplete, show the best evidence and the active
routing path, then let indexing/recovery continue in the background.

### Past lapses in this project (the receipts)

These are real mistakes the project has made — recorded here so
future contributors see the pattern and don't repeat it.

| Lapse | Anti-pattern instance | Principled fix | Released in |
| --- | --- | --- | --- |
| **code_graph.py** | hardcoded Rust + Python + JS + TS regex extractors as the sole way to build the file-export graph; new languages required new regex branches inside the retrieval module | `reference_graph.register_extractor(name, extensions, fn)` — the abstraction is now "A references B"; `code_graph.py` is a 75-line back-compat facade; new content types (markdown shipped, YAML / knowledge-graph / Obsidian one line away) plug in without touching retrieval | `0.2.0` |
| **mxbai-embed-large substrate** | English-and-code-only embedder ranked re-export aggregators above canonical implementations; Chinese / mixed-language code comments performed poorly | `bge-m3` substrate (multilingual XLM-RoBERTa, symmetric, 8 k context); query and passage share the same vector space; new languages cost zero code | `0.2.0` |
| **symbol_channel.py** | tree-sitter symbol extraction only knows Rust / Python / JS / TS; adding Go / Ruby / Java requires installing new grammars + extending `symbol_kinds_for_language()`; markdown / PDF / YAML get nothing | tracked as **Phase C path B** (`docs/plans/2026-05-05-phase-c-audit.md`): generalise to a `register_structural_extractor` registry covering code symbols, markdown headings, PDF sections, YAML keys, etc.; the router's `looks_like_structural_ref(query)` becomes content-type-agnostic | open (slated for `0.3.x`) |
| **intelligent_cli._METADATA_TOKENS** | hand-curated set of recency / size / listing keywords (`recent`, `latest`, `最近`, `最新`, …) used to detect out-of-scope queries; the user reported `我昨天打开过的十个文件` → not flagged because `昨天` was missing; patched in `0.2.5` by adding `昨天 / 今天 / 前天 / 上周 / 本周 / 打开过 / 改过 / yesterday / today / this week / last week`; this is a patch, not the answer | `0.2.6`: `RouterDecision` gained an `out_of_scope` field (`none` / `recency` / `size` / `listing`); the existing `llm_router.route_query()` LLM prompt now classifies scope on the same call that's already running for retrieval intent — zero added latency. `intelligent_cli.detect_out_of_scope` consults `decision.out_of_scope` first; the keyword list is now strictly an **offline safety net** for when Ollama is unreachable | `0.2.6` ✓ shipped |
| **proactive.filename_extend_should_fire** | 0.2.7 shipped the proactive framework but its built-in gate enumerated English / Chinese natural-language lookup phrases (`"where is" / "find me" / "在哪" / "找一下" / "我的"`) as a fallback when ``decision.intent`` was not ``"filename"``. The user caught this on the same day: "I see you're still using a lot of these keyword phrases. We shouldn't use keywords." This was the **third** Principle-1 lapse against the same anti-pattern in this project | `0.2.8`: gate trusts ``decision.intent`` exclusively. When LLM router (or its rule-based fallback) classifies intent as ``filename`` / ``mixed`` → fire; anything else → don't. ``decision is None`` means "no understanding available" and refuses to fire rather than enumerate. The LLM is the only source of intent truth | `0.2.8` ✓ shipped |
| **proactive gate iteration (0.2.9 → 0.2.10)** | 0.2.8's strict ``intent ∈ {filename, mixed}`` gate rejected the LLM-unreachable case where rule-based fallback returned ``intent=lexical, primary_token=""``. 0.2.9 added a third eligibility case based on token-shape morphology (``_looks_like_identifier``) — and the user immediately caught this as the same anti-pattern in different clothes ("我不是要什么关不关键的短语就是说你现在不是有一个intent吗任何的intent如果当前的问题识别不了或者在当前的问题下识别不了应该触发"). **Fourth Principle-1 lapse.** | `0.2.10`: gate is purely results-based. ``not results`` → fire; ``results present + primary_token + no basename match`` → fire; everything else → don't. Token-shape / morphology decisions moved INTO ``filename_extend_execute`` where they shape the *mechanism* (which token to ``find`` for, return None if none usable) but never gate *eligibility*. The cleanest realisation of "policy = did scope fail; mechanism = how to extend scope" | `0.2.10` ✓ shipped |
| **proactive `find` budget bug (0.2.7 → 0.2.10)** | 0.2.7-0.2.9 divided the per-enhancer budget by the number of search dirs (``per_dir_s = budget / N``), even though the dirs run in **parallel** threads. A 400 ms budget across 3 dirs gave 133 ms per ``find`` — under the typical ``~/Downloads`` ``find`` time of 161 ms. ``find`` got SIGKILLed seconds before yielding its output, returning 0 hits despite matching files existing. Three releases of "the gate fires, why is there no output?" came from this. **Lesson:** end-to-end time-the-actual-thing before shipping. Unit tests on the gate alone don't catch mechanism timing bugs | `0.2.10`: ``per_dir_s = max(0.2, individual_budget_ms / 1000.0)`` (no division). Defaults bumped to ``DEFAULT_TOTAL_BUDGET_MS = 2000``, ``filename_extend.individual_budget_ms = 1500``. End-to-end verified before tagging: 1093 ms wall clock to surface 4 actual user-reported files from the user's real home dirs | `0.2.10` ✓ shipped |
| **single-intent routing (0.5.8.x)** | A query like `show where my project report that I recently created in case42 folder` can contain target, scope, metadata, and answer-depth facets at once. Treating the fast-intent result as one terminal label lets `metadata` suppress filename anchors, lets `filename` suppress semantic depth, or lets a missing scope fall back to broad hidden/tool-directory sweeps. | Query planning is now facet-based: scope is resolved to a real bounded root before search; metadata is a ranking/filter modifier unless metadata-only; hidden/cache/tool directories are excluded from lazy seeds; filename evidence only ends foreground work when it satisfies the requested answer depth. The small model proposes facets, but filesystem evidence decides finality. | `0.5.9` |

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

Foreground work should stop only when the current query's **answer
depth** is satisfied. A concrete filename hit is enough for a path
question; the same hit is enough for `--detail full` because render-time
lazy extraction can read the concrete file body directly. It is not
enough for `--answer`, agentic, or semantic-content questions; those
paths must keep the semantic/cascade layer alive. Global indexing and
recovery may continue in the background after any fast foreground answer.
Filename finality also requires an independent fast-intent confirmation
that the query is path-depth; if the substrate is uncertain or sees a
semantic information need, the filename match stays as an anchor and
retrieval continues.
Conversely, semantic-depth queries that contain a path-like filename
clue should keep the filename tier enabled: the filename hit is an
anchor for retrieval, not a competing intent that semantic search must
choose against.

---

## Principle 4 — End-to-End Means Every Public Surface

`docs/RELEASING.md` codifies the surfaces every release touches. The
word "release" means the whole chain: current codebase committed,
tagged, pushed to GitHub; PyPI uploaded and verified through JSON,
simple index, and clean-venv install; GitHub Release created with
artifacts; GitHub README and GitHub Pages home/changelog/release pages
rendered and verified; managed `skygrep setup` instructions kept current
for existing agent integrations; local editable install refreshed to the
same version. The 0.2.2 → 0.2.3 lapse (PyPI shipped, GitHub Pages
silent) is the receipt; the checklist is the prevention. No release is
done until every surface is updated and the privacy gate is clean.

---

## Principle 6 — Proactive over Passive

**The anti-pattern:** when the system can't answer the user's
query under its current bounded scope, it shrugs — "no matches",
"index is building", "try a different query later". The user has
to guess what to do next, possibly across multiple invocations,
sometimes hitting `Ctrl-C` because they think the tool is broken.

**The pattern:** the system should **try extra work in parallel
within a strict latency budget** to surface help the user can act
on. If the answer is "no match in this directory", surface what
the answer would be in a likely alternative directory. If the
answer is "low-confidence top hit", surface the confidence and
suggest a refinement. If the top hit is a markdown file, surface
its linked references. **Proactivity is not optional; it's the
default — bounded by latency, gated by should-fire, and
content-agnostic by construction.**

### Bounds (the contract)

Proactive work is only acceptable when ALL these hold:

  1. **Bounded latency.** Total wall-clock cap (default 500 ms,
     `SKYGREP_PROACTIVE_BUDGET_MS`, currently 2000 ms by default);
     each enhancer also has its
     own `individual_budget_ms`. The runner uses
     `ThreadPoolExecutor.shutdown(wait=False, cancel_futures=True)`
     so over-budget work doesn't bleed into the user's perceived
     latency.
  2. **Gated by should-fire.** Each enhancer declares cheap
     conditions under which it's worth running. We don't pay the
     budget for enhancers that aren't going to produce useful
     output. Should-fire is O(1) on already-computed inputs
     (query, decision, results) — no I/O, no LLM calls.
  3. **Content-agnostic by registry.** New enhancers plug in via
     `register_enhancer()` — same architectural shape as
     `reference_graph.register_extractor()` (Principle 1).
     Filename-extend is content-agnostic by accident; markdown
     link-traversal, PDF section extraction, git-history
     traversal, query refinement etc. are all eligible plug-ins.
  4. **Failure-isolated.** One enhancer raising / hanging /
     misbehaving must NOT break the others. The runner runs each
     in its own thread + catches all exceptions + drops their
     result silently.
  5. **Killable.** `SKYGREP_NO_PROACTIVE=1` (or
     `SKYGREP_NO_HINTS=1`) disables the whole framework so users
     who need a quiet CLI / strictly-deterministic CI can opt
     out.
  6. **Answer-depth aware.** Proactive results may complete the
     foreground request only when their structured evidence satisfies
     the user's requested depth. Path lookup can finish on concrete
     filename evidence; full-detail lookup can finish after lazy
     extraction from that concrete file; synthesized answers and
     semantic-content questions must continue through the semantic
     layer.

### What proactive should NOT do

  - **Latency creep on the common case.** The 95 % of queries
    where the cascade returned good results should pay zero
    extra cost. The should-fire gate is the protection.
  - **Mutate state silently.** Proactive enhancers produce
    *suggestions* and *additional read-only results*. They do not
    create files, run shell commands, modify the index, etc.,
    without an explicit user confirmation step (which we do not
    yet provide; future enhancers requiring action must ask).
  - **Replace semantic recall.** Proactive output is additional
    evidence, never a permission slip to suppress recall without
    evidence. It may render before cascade when it has already
    satisfied a path-depth query, but it must not terminate a
    semantic or synthesized-answer path.

### Receipts (proactive enhancers shipped)

| Enhancer | What | Released in |
| --- | --- | :-: |
| **`filename_extend`** | When the generic intent substrate routes to `intent=filename` and the current search scope lacks concrete basename evidence, run bounded parallel filename expansion across configured home roots (default common user document roots, overridable by `SKYGREP_PROACTIVE_DIRS`). Results use the same `filename-lookup` schema as in-scope filename search, so path answers return fast and `--detail full` can lazily extract content from the matched file without waiting for semantic indexing. | `0.2.7` ✓, refined through `0.5.8.x` |
| `query_refinement` (open) | When the cascade returns top-1 < floor AND σ-gap < floor, ask the LLM router for a refined query suggestion. Bounded by individual_budget_ms ≤ 400 ms. | open |
| `markdown_link_traverse` (open) | When a top hit is a `.md` file, surface notes linked from it via `extractors.markdown` (already shipped in 0.2.0). Pure SQL on the existing reference graph; budget ≤ 100 ms. | open |
| `pdf_section_extract` (open) | When a top hit is a `.pdf`, surface section titles. Reuses the 0.1.0 `pypdf` extraction. Budget ≤ 300 ms. | open |
| `git_history_related` (open) | When a top hit is in a git repo, surface the last 5 commits that touched the same file. Budget ≤ 150 ms. | open |

### The rule (for every enhancer PR)

Before adding a new enhancer:

  1. State the **should-fire** signal that gates it. Cheap, O(1).
  2. State the **individual budget** in milliseconds. Justify.
  3. Confirm it doesn't mutate state.
  4. Add a contract test in `tests/test_proactive.py`
     demonstrating that should-fire returns False on the common
     case (so the enhancer doesn't bleed budget on every query).

If the enhancer wants to do ONLINE work (LLM call, network), the
individual budget must be measured against `qwen2.5:3b`'s 90th
percentile response time, not its mean.

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

## Principle 7 — Recall substrate before route certainty (0.5.13+)

The intent router is allowed to rank retrieval lanes; it is not allowed
to be the only component that decides which files are visible. Every
semantic or mixed query should get a cheap, generic candidate recall
substrate before the expensive cascade makes its final choice.

The substrate must be independent and bounded:

  - path-token recall from indexed paths;
  - symbol recall from indexed names;
  - chunk-token recall from SQLite;
  - bounded `rg -il -F` recall under the requested scope;
  - explicit include-scope recall when the caller already knows a
    folder or file boundary.

Candidate recall is an additive evidence lane, not a hard answer. It can
constrain semantic search only when there is enough evidence or an
explicit include scope, and it still contributes lexical evidence chunks
so a downstream LLM can see why the path was selected.

For content/agent calls, one chunk per file is often not enough. The
retriever may attach a small per-file evidence pack containing related
symbol, constant, or assertion anchors from the same recalled file. Keep
that pack bounded by requested depth: no support pack for brief/summary
location output, compact support for standard content, and wider support
only for full-depth reads.

---

## How this document gets used

  - `CLAUDE.md` imports this file via `@docs/PRINCIPLES.md` so any
    Claude session in this repo loads the principles automatically.
  - Human contributors should read this once on first PR; the rule
    in **Principle 1** is the most likely tripwire.
  - Update this file whenever a new architectural lapse is
    identified — receipts go in the table above; the rule and
    pattern stay stable.
