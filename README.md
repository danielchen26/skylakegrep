<p align="center">
  <img alt="skylakegrep — fully-offline semantic search over your local files" src="docs/assets/hero-dark.svg" width="100%">
</p>

<p align="center">
  <a href="https://pypi.org/project/skylakegrep/"><img src="https://img.shields.io/pypi/v/skylakegrep?label=pypi&color=22d3ee&labelColor=0a0d12" alt="PyPI"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.9%2B-22d3ee?labelColor=0a0d12" alt="Python 3.9+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-PolyForm--NC--1.0.0-f59e0b?labelColor=0a0d12" alt="PolyForm Noncommercial 1.0.0"></a>
  <a href="https://danielchen26.github.io/skylakegrep/"><img src="https://img.shields.io/badge/docs-published-22d3ee?labelColor=0a0d12" alt="Documentation"></a>
  <a href="https://github.com/danielchen26/skylakegrep/releases/latest"><img src="https://img.shields.io/github/v/release/danielchen26/skylakegrep?label=release&color=22d3ee&labelColor=0a0d12" alt="Latest release"></a>
</p>

<p align="center">
  <a href="#install"><b>Install</b></a>
  &nbsp;·&nbsp;
  <a href="#three-ways-people-use-it"><b>Scenarios</b></a>
  &nbsp;·&nbsp;
  <a href="#new-in-05x"><b>New in 0.5.x</b></a>
  &nbsp;·&nbsp;
  <a href="#why-skylakegrep"><b>Why?</b></a>
  &nbsp;·&nbsp;
  <a href="#how-it-works"><b>How it works</b></a>
  &nbsp;·&nbsp;
  <a href="#performance"><b>Benchmarks</b></a>
  &nbsp;·&nbsp;
  <a href="https://danielchen26.github.io/skylakegrep/"><b>Docs site</b></a>
</p>

---

# Find anything on your machine.

> **Smart semantic search, fast enough to feel instant.** Ask in
> plain English — or any of 100+ languages — and get back the
> right file and line range in about a second, even when the
> working directory isn't the right project. Fully offline.

**Semantic search for code, PDFs, notes, and docs.** Fully offline.
No cloud. No telemetry. No subscription. Ask in plain English (or
any of 100+ languages) and get the right file + line range. Scoped
location and lexical-friendly queries usually return sub-second; deeper
semantic retrieval stays bounded and reports its route while it works.

```console
$ skygrep "where does the auth token get refreshed?"

═══ auth/middleware.py:78-94          score 0.91 · python
async def renew_session(req: Request):
    # swap the access cookie when the refresh JWT is still valid
    if req.cookies.get("rt") and access_expired(req):
        return await refresh_token(claims, key)

╰─ done   0.5s · quality=BEST
   path     : cosine-cheap (high-confidence early-exit)
   evidence : σ-gap=0.082 ≥ τ=0.005 (adaptive)
```

[**Install in 30 s →**](#install) &nbsp;·&nbsp;
[How it works →](#how-it-works) &nbsp;·&nbsp;
[Benchmarks →](#performance)

> **30 / 30** public-OSS recall (fully-indexed) &nbsp;·&nbsp;
> **+30 %** lazy auto-trigger over `rg` cold-start (0.5.3) &nbsp;·&nbsp;
> **bounded wrong-path discovery** via proactive umbrella &nbsp;·&nbsp;
> **~1 s** warm queries &nbsp;·&nbsp;
> **100 %** local &nbsp;·&nbsp;
> **44** releases shipped

---

## Three ways people use it

### 🧠 Code by concept

Find code by what it does, not what it's called. The semantic
substrate (`bge-m3`) bridges your phrasing to the actual identifier
even when the function name uses different words.

```console
$ skygrep "where does session refresh logic live?"

→ auth/middleware.py:78  ·  renew_session()
```

> No `rg` hit for *"session refresh"*; semantic retrieval bridges to
> `renew_session` from the project index.

---

### 📄 Cross-content

One query across code, PDFs, notes, and docs. Markdown, PDF,
Word, plain text — all indexed via the same content-agnostic
substrate. Your query searches all of them at once, ranked by
semantic relevance.

```console
$ skygrep "the design doc on rate limiter rewrite"

→ docs/rate-limiter-redesign.md  ·  designs/q3-rewrite.pdf
```

> Markdown link graph + PDF text-layer extraction in one cascade.

---

### 🌐 Multilingual · private

`bge-m3` understands 100+ languages out of the box. Index,
retrieval, ranking, optional answer synthesis — all run locally
via Ollama. Zero network calls.

```console
$ skygrep "我昨天写的 cascade 调度代码"

→ src/storage.py:847  ·  cascade_search()
```

> Mixed Chinese / English query. Zero network. Audit-friendly.

---

## New in 0.5.x

Four qualitative leaps since 0.4 — the through-line is **less
ceremony from you, more intelligence from the tool.**

### 🚀 Just ask — no `skygrep index .`

The first query in a fresh repo works. A background process builds
the semantic index while a `rg` fallback handles your first turn;
from the second query on, the full cascade is online.

```console
$ cd /path/to/brand-new-project
$ skygrep "how does auth handle expired tokens?"

→ src/auth/token.py:140  ·  refresh_or_redirect()
```

> Cold-start vocabulary-mismatch: **0/10 → 4/10** over plain `rg`
> on the Django oracle bench (0.5.3, real-CLI verified).

---

### 🧭 Smart from the wrong folder

Run skygrep from `/tmp` and ask about a real project. The router
dispatches **two retrieval lanes in parallel**; a proactive umbrella
that searches sibling roots in `SKYGREP_PROACTIVE_DIRS` can answer
before the cascade has time to run its first rerank.

```console
$ cd /tmp/scratch
$ skygrep "where does the parallel umbrella dispatch?"

→ ~/code/skylakegrep/src/cli.py:912  ·  cascade ‖ proactive umbrella
```

> Wrong-cwd discovery is bounded. Set `SKYGREP_PROACTIVE_DIRS` or pass
> an explicit scope when an agent already knows where to look.

---

### 🧠 Streaming intelligent routing

Each query is classified by a local LLM router (`qwen2.5:3b`) for
intent / scope / primary token, then dispatched to multiple lanes
in parallel. Each result lands tagged with the route it came from
and the still-searching status of the others — never silent, always
honest about what's pending.

```console
$ skygrep "the design doc on rate limiter rewrite"

├─ proactive umbrella · filename glob
│             cascade still searching
═══ docs/rate-limiter-redesign.md:1

╰─ done   0.4s · quality=BEST
   path   : proactive + cascade
   router : llm -> intent=mixed
```

> Confidence-streaming: results stream as they're ready, tagged with
> the route they came from. Each answer's provenance is auditable.

---

### 🔍 Why this matched · `skygrep -x`  *(new in 0.5.8)*

Every retrieved chunk now carries the full provenance of how it got
there. Pass **`--explain`** (or **`-x`**) and skygrep prints a one-line
**router rationale** at the top, a **per-result `via:` line** under
each header showing which channel(s) contributed, and a **cascade-lane
summary** showing the σ-adaptive evidence at the bottom. No new model
calls, no extra retrieval — every signal was already in the pipeline.

```console
$ skygrep -x "find pyproject.toml in this repo"

├─ route      router: filename · primary_token="pyproject.toml" · conf=0.95 · source=llm
│             reason: "user is looking for a specific file by name in the repo"

╭─ pyproject.toml ────────────────────────────────── [toml]  1.000
│ via: filename-lookup · token "pyproject.toml" · score=1.000
│
│ size:  1.0 KB    modified: 2026-05-06 16:51    type: toml
╰──────────────────────────────────────────────────────────────────

├─ cascade   lane: cosine-cheap (gap=0.037, tau=0.016)
```

> Three layers answer three different "why" questions:
> **what intent** the LLM router inferred, **which channel** retrieved
> this chunk (cosine cascade · symbol RRF · filename-lookup · ripgrep
> shortcut), and **which lane** answered. Bonus 0.5.8: if Ollama isn't
> running, skygrep starts it in the background and tells you — no more
> silent rule-based fallbacks.

---

## Why skylakegrep?

Sized against **four named alternatives**, not generic categories.

<p align="center">
  <img alt="skylakegrep — comparison matrix vs ripgrep, mgrep (predecessor), autodev-codebase, Sourcegraph Cody" src="docs/assets/comparison-matrix.svg" width="100%">
</p>


---

## How it works

<p align="center">
  <img alt="skylakegrep — router + two parallel retrieval lanes (cosine cascade ‖ proactive umbrella with filename_extend, lazy_cwd, lazy_cross_folder, streaming dispatcher)" src="docs/assets/workflow-diagram.svg" width="100%">
</p>

**Local Ollama + SQLite. Zero network calls. Zero subscription.**
The same architecture handles every content type — code · PDFs ·
notes · markdown · any file you register an extractor for.

The LLM router classifies *intent + scope + primary token* on every
query. Two retrieval lanes then **race in parallel** — not in
series:

  - **σ-adaptive cosine cascade** — when the working directory is
    indexed and right, `bge-m3` (multilingual, 1024-d, symmetric
    XLM-RoBERTa) ranks files; high-confidence queries early-exit
    on cheap cosine, uncertain ones escalate to a cross-encoder
    rerank. A tree-sitter symbol channel and hybrid lexical RRF
    fusion fold in alongside, with a reference-graph PageRank
    tiebreak.
  - **Proactive umbrella** — four tiers run concurrent with the
    cascade (not after it): `filename_extend` for fast filename
    matching, `lazy_cwd` for auto-indexing the current folder,
    `lazy_cross_folder` for sibling roots in
    `SKYGREP_PROACTIVE_DIRS`, and a streaming dispatcher that
    posts each answer as it lands.

The first confident answer streams to your terminal — refinements
arrive as later lanes finish. ~1 s typical, even when the working
directory is the wrong project (0.5.7, real-CLI verified).

[Architecture deep-dive →](https://danielchen26.github.io/skylakegrep/)

---

## How skylakegrep differs from Elasticsearch

<details>
<summary><b>For people asking "why not just use ES?"</b></summary>

**Different niche, different design.** Elasticsearch is a multi-tenant,
TB-scale, distributed search engine for data centers. skylakegrep is
a single-user, single-machine, zero-ops CLI for a developer asking
their own laptop a question. Both can be called "search engines";
they answer different problems.

| | skylakegrep 0.5.x | Elasticsearch |
|---|---|---|
| **Setup** | `python3 -m pip install --user skylakegrep`; cold-start lazy auto-trigger | JVM, cluster, mappings, ingest pipeline, dense-vector plugin, reindex |
| **Semantic retrieval** | bge-m3 (1024-d, 100+ languages) via local Ollama, out of the box | Manual: pick embedder, pipeline, dimension, reindex |
| **Intent understanding** | qwen2.5:3b LLM router classifies intent / scope / primary token per query | None natively; you write query DSL by hand |
| **Code AST awareness** | tree-sitter symbol channel, RRF-fused with cosine | None; code is plain text |
| **Cold-start / wrong-folder** | `lazy_cwd` + `lazy_cross_folder` 4-lane parallel umbrella, ~1.1 s | Empty index = 0 results |
| **Why-this-matched explainability** | `--explain` shows router rationale + channel breakdown + lane evidence | BM25 highlight only |
| **Cross-file context** | reference-graph PageRank tiebreak | None |
| **Privacy / offline** | 100 % local by design | Index can be local, but most embeddings are external API calls |
| **Latency p95 (single repo, 50k files)** | 0.3 – 1.1 s including LLM router | ms-level *after* you've paid the operational cost |
| **Scale** | single-machine, single-repo sweet spot | billions of docs, multi-shard, distributed |
| **Multi-tenant / ACL** | not designed for this | first-class |
| **Aggregations / facets / time-series** | not designed for this | first-class |
| **Operational cost** | zero (no daemon, no GC tuning, no shard rebalance) | non-trivial (GC, heap, shard rebalance, monitoring) |

**Where skylakegrep wins:** "I just opened my terminal and want to find
something on my own machine." Easier, more semantic, more code-aware,
more private — and now (0.5.8) it can also tell you *why* it picked
each result.

**Where Elasticsearch wins:** anything that needs scale, multi-tenant
isolation, faceted aggregations, or production-grade replication.
We don't try to compete in those rooms.

> ES is the search engine of the data center. skylakegrep is the
> search engine of your developer terminal.

</details>

---

## Install

```bash
# 0. confirm Python 3.9+ is available
python3 --version

# 1. install with the same Python that will own the CLI
python3 -m pip install --user skylakegrep

# 2. pull the local models, one command per model
ollama pull bge-m3
ollama pull qwen2.5:3b

# 3. verify runtime, models, install path, and index state
skygrep doctor

# 4. (one time) register skygrep with your LLM CLI of choice
skygrep setup     # Claude Code · Codex · OpenCode · Gemini CLI · Cursor

# 5. ask anything, anywhere
skygrep "your question here"
```

`skygrep setup` writes a short agent rule into Claude Code, Codex,
OpenCode, Gemini CLI, and Cursor when detected. The rule tells the
agent which depth to request: bare `skygrep` for location/concept
lookups, `--content` for source snippets, `--detail full` only after
narrowing, `--answer` for local synthesis, and `--json` for
machine-readable tool calls. Re-running `skygrep setup` refreshes the
managed block when these instructions improve. After an upgrade, normal
`skygrep` searches and `skygrep doctor` also refresh already-registered
managed blocks automatically; new integrations still require an explicit
`skygrep setup`.

On macOS, `python` may not exist; use `python3`. If `skygrep` installs but
the shell cannot find it, inspect:

```bash
python3 -m site --user-base
which -a skygrep
python3 -m pip show skylakegrep
```

The user-site script commonly lives under
`~/Library/Python/3.x/bin/skygrep`; add that `bin` directory to `PATH`
or use a virtual environment.

```bash
export PATH="$(python3 -m site --user-base)/bin:$PATH"
```

If setup gets tangled across multiple Python installs, reset cleanly:

```bash
# Remove LLM-CLI snippets written by `skygrep setup`.
skygrep setup --uninstall || true

# Remove the Python package from the Python that installed it.
python3 -m pip uninstall -y skylakegrep

# Optional: delete local indexes/config. This does not delete your files.
rm -rf ~/.skylakegrep

# Optional: remove downloaded Ollama models.
ollama rm bge-m3
ollama rm qwen2.5:3b

# Reinstall from a clean state.
python3 -m pip install --user --no-cache-dir skylakegrep
ollama pull bge-m3
ollama pull qwen2.5:3b
skygrep doctor
```

That's it. The first query in a fresh project completes in under
a second via a `ripgrep` fallback while a background process
builds the semantic index. Every query after that uses the full
cascade with the local LLM kept warm in memory.

---

## Performance

Public-OSS reproducible benchmark across three popular codebases
(Django · React · Tokio · 30 hand-labelled questions, 10 each):

<p align="center">
  <img alt="skylakegrep — public-OSS benchmark performance (30/30 recall on Django + Tokio + React)" src="docs/assets/performance-matrix.svg" width="100%">
</p>


Honest reading:

  - `rg`'s 100 % is a recall-ceiling baseline — it returns 20 M+
    tokens per query (term-OR scan with 2-line context windows).
    Yes, the answer is in the dump; no, the agent has to read all
    of it to find it.
  - **skygrep returns the right file ranked top-10 in 30 / 30 cases**
    while emitting 60 × – 770 × less context for the agent's LLM
    round-trip downstream. That's the user-facing number.
  - Reproduce: `git clone` Django + React + Tokio at any commit,
    run `benchmarks/public_oss_bench.py`. Numbers within ±5 %.

For the full bench protocol, per-task analysis, and worked
example (one query · 1,395 × token reduction), see
[`docs/parity-benchmarks.html`](docs/parity-benchmarks.html).

### Agent tool-context benchmark (0.5.13)

0.5.13 adds a benchmark for the exact middle-layer problem that LLM
agents face: "How much compact, ranked evidence did the tool return
before the agent continues reasoning?" It compares one structured
`skygrep --json --content` call per task against a simulated raw-`rg`
agent that runs several term searches and line-window reads.

| Metric | `skygrep-agent` | raw `rg-agent` |
| --- | ---: | ---: |
| Tasks × effort profiles | 24 | 24 |
| Path coverage | 81.9 % | 100.0 % |
| Path precision | 34.9 % | 12.2 % |
| Evidence coverage | 79.2 % | 92.7 % |
| Sufficiency score | 80.8 % | 97.1 % |
| Tool calls | 24 | 147 |
| Context tokens | 56,424 | 2,129,655 |
| Sufficiency per 1k tokens | 0.344 | 0.011 |

Honest reading: raw `rg` remains the recall ceiling and can be faster
when dumping large unranked context is acceptable. skygrep uses
**6.12× fewer tool calls**, emits **37.74× less context**, and gives
the downstream LLM **31.27× denser sufficiency per token**. That is the
agent-facing win: enough evidence for the next reasoning step without
making the model sift through a repository-sized haystack.

---

## What you can search

The retrieval substrate is **content-agnostic** by design. The
embedder, the cascade, and the reference graph all abstract over
"A references B" — not over any specific programming language or
file format. New content types plug in via a one-line
`register_extractor()` call.

<p align="center">
  <img alt="skylakegrep — six content types: code, markdown, PDF, Word docs, plain text family, and your custom type via register_extractor" src="docs/assets/content-types.svg" width="100%">
</p>


```python
from skylakegrep.src.reference_graph import register_extractor

def yaml_anchor_extractor(path):
    """Return list of (source, target) reference edges."""
    ...

register_extractor("yaml", [".yaml", ".yml"], yaml_anchor_extractor)
```

---

## Command cheatsheet

The **bare form** — `skygrep "<your question>"` — covers ~95 % of
real-world use. No subcommand, no flags. The system auto-routes
(LLM router → `find` / `rg` / semantic cascade), auto-indexes on
first query, and auto-recovers when the embedder is upgraded.

<p align="center">
  <img alt="skylakegrep — CLI cheatsheet (bare form featured, 8 secondary commands as tiles)" src="docs/assets/cli-cheatsheet.svg" width="100%">
</p>

### Choose the right information depth

The same natural-language question can ask for different levels of
evidence. Keep the first query cheap; only ask for more depth when the
task needs it.

| Goal | Command |
|---|---|
| Locate the file or folder quickly | `skygrep "where is the project brief I edited recently?"` |
| Show relevant source/document snippets | `skygrep --content --detail standard "what does the API migration plan say about rollback?"` |
| Read deeper after narrowing to one path | `skygrep --content --detail full --include "docs/migration-plan.md" "show the deployment steps"` |
| Quick deep-read shorthand | `skygrep --detail "show the deployment steps"` |
| Synthesize a local answer from retrieved evidence | `skygrep --answer --content "summarize the payment retry policy"` |
| Feed compact structured context to an LLM agent | `skygrep --json --content --detail standard --include "src/**" "where is token refresh implemented?"` |
| Audit why a route/result was chosen | `skygrep --explain "where is token refresh implemented?"` |

Agent rule of thumb: run from the relevant project root, or pass
`--include` / `--lexical-root` when the scope is known. Start bare for
**where / locate / which file** questions; add `--content` for **what
does it say / explain / summarize** questions; add `--json` whenever
another LLM will consume the result. Avoid broad home-directory semantic
queries unless the user really wants cross-folder discovery; they are now
bounded, but scoped queries are both faster and more accurate.

### Reading the per-query telemetry footer (0.2.2+)

Every search prints a structured workflow footer so you can see *which*
retrieval path answered your query and *why* without parsing a long line:

```
╰─ done   0.42s · quality=BEST
   path     : cosine-cheap (high-confidence early-exit)
   router   : llm -> intent=mixed (0.83)
   evidence : σ-gap=0.0820 ≥ τ=0.0050 (adaptive)
   pool     : 1 filename + 0 lexical · cascade
   index    : 20s ago · 36 files · L2 symbols + graph prior
```

Field guide:

  - **`path=`** — `cosine-cheap` / `cosine-escalated-rerank` /
    `rg-only` / `cascade-skipped`. The retrieval strategy this
    specific query took.
  - **`σ-gap=… → reason`** — Bayesian-evidence proxy that drove
    the cascade decision. High σ-gap = top-K candidates well
    separated → cosine trusted, exit cheap. Low σ-gap = candidates
    tied → escalate to rerank.
  - **`recovery=…`** *(only when the recovery worker is active)* —
    live progress + ETA for the in-progress re-embed.
  - **`quality=BEST` / `DEGRADED-recovery`** — at-a-glance trust
    indicator.

---

## Configuration

Set via environment variables. Defaults work — tune only when you
need to. Grouped into three panels: Ollama setup, Indexing & rerank,
Behavior toggles.

Cold-start lazy semantic search is intentionally budgeted. If a first
query says it hit the foreground budget, either scope the query
(`--include "docs/**"` / run from the right project root) or
raise the foreground knobs:

```bash
export SKYGREP_COLD_LAZY_TOTAL_BUDGET_S=15
export SKYGREP_COLD_LAZY_CWD_BUDGET_S=10
export SKYGREP_COLD_LAZY_CROSS_BUDGET_S=4
export SKYGREP_COLD_LAZY_SEED_BUDGET=24
```

The default stays conservative so broad home-folder searches cannot
block the terminal for minutes. Background indexing continues after the
foreground budget expires.

Interactive terminals animate only the narrow left workflow rail during
foreground semantic waits. The rail uses a three-cell particle stream with
blue/cyan/white coloring; captured output, `--json`, logs, and agent
tool calls stay stable. Turn it off if you prefer fully static progress:

```bash
export SKYGREP_UI_ANIMATION=off
```

The result workflow rail stays compact and copyable by default. To force
an alternate rail:

```bash
export SKYGREP_UI_RAIL=tree    # or: helix
```

The `helix` rail replaces box connectors with a denser three-cell rotating
particle field (`• ·`, ` ·•`, `· •`, `•· `) plus a slim separator line, so
the workflow itself reads like one continuous vertical particle stream
through progress, results, and the final routing footer.

Interactive terminals show Nerd Font step icons by default. Disable them
if your terminal font does not support patched glyphs:

```bash
export SKYGREP_UI_ICONS=off
```

Captured output and agent/tool paths keep plain labels unless icons are
explicitly requested.

<p align="center">
  <img alt="skylakegrep — environment variable configuration grouped into Ollama setup, Indexing &amp; rerank, and Behavior toggles" src="docs/assets/configuration.svg" width="100%">
</p>


---

## Release history

**Recent releases** (in reverse chronological order):

  - **`0.5.13`** — Adaptive candidate recall for agent-grade context.
    Semantic and mixed queries now get a bounded, generic recall
    substrate before final cascade ranking: explicit include scope,
    indexed path tokens, indexed symbols, SQLite chunk text, and a small
    `rg -il -F` lane can all vote files into the candidate pool.
    Content/agent calls receive a bounded same-file support pack so the
    downstream LLM sees proof, not just paths. Module-level text,
    constants, and long string anchors are indexed more reliably. The new
    agent tool-context benchmark shows 6.12× fewer tool calls, 37.74× less
    context, and 31.27× higher sufficiency density than a raw-`rg` agent
    baseline on 24 generic depth tasks.
  - **`0.5.12`** — Bounded cold semantic routing and public example
    verification. Cold-start semantic search now enforces real
    foreground budgets for cwd and cross-folder lazy lanes, prunes
    hidden/dependency-cache trees before descent, runs ripgrep fallback
    in one bounded pass, and avoids cross-folder pollution when local
    semantic evidence exists. Agent examples now show scoped
    `--include` usage, relative include globs work against absolute
    index paths, and `--answer` no longer adds missing-evidence caveats
    when retrieved sources directly answer.
  - **`0.5.11`** — Fast scoped discovery plus Python 3.10 CI hotfix.
    This supersedes 0.5.10: the scoped descriptor + metadata
    file-discovery lane, background refresh deferral, honest wall-time
    footer, and automatic setup-instruction refresh remain the headline
    behavior, and the proactive enhancer now handles Python 3.10
    `concurrent.futures.TimeoutError` budget exhaustion as telemetry
    instead of surfacing it as an exception in CI.
  - **`0.5.10`** — Fast scoped file discovery and agent
    instruction depth. Scoped file-location queries that combine a
    concrete folder, target descriptors, and metadata modifiers now use
    a generic filesystem-evidence lane before semantic cascade, so
    path-depth answers can return in sub-second time without sacrificing
    semantic depth for content/answer queries. Large foreground refreshes
    defer to background indexing, footer timing now reports command wall
    time, and `skygrep setup` teaches Claude Code / Codex / OpenCode /
    Gemini / Cursor the information-depth ladder (`--content`,
    `--detail full`, `--answer`, `--json`). Existing managed setup
    snippets auto-refresh after upgrade without touching user-authored
    text.
  - **`0.5.9`** — Generic adaptive routing and scoped search
    performance. Scope is now a first-class query-plan facet: folder /
    repo / workspace clauses are resolved to a concrete root and stripped
    from router text before fast-intent, LLM fallback, metadata, and
    lexical gates run. Metadata remains instant when it fully answers the
    query, but acts only as a modifier when the user also names a target.
    Scoped semantic and JSON/agent queries can now finish from strong
    lexical evidence without waiting for expensive cascade/rerank, while
    CJK and mixed-language scope forms such as `在合同档案文件夹...` are handled
    generically. Release validation covered 12 synthetic CLI cases, all
    under 0.9 s after the fix, with no private examples in public surfaces.
  - **`0.5.8.7`** — Adaptive query-plan routing. Filesystem
    metadata is now a `metadata_kind` / `metadata_terminal` facet
    instead of a mutually exclusive intent: pure metadata queries
    still return immediately, while composite queries use metadata
    only as a modifier/reranker and continue through filename,
    lexical, and semantic retrieval. Adds `created` metadata,
    CJK terminal/modifier separation, a code-identifier collision
    guard for tokens such as `created_at`, optional document
    evidence fields for JSON/agent paths, and expands the privacy
    release scan to benchmark files.
  - **`0.5.8.6`** — Fast path answers without sacrificing semantic
    depth. Human output now shows the active router lane by default,
    filename hits are final only for path-depth questions, and semantic
    queries that contain a filename-like clue keep that file as an
    anchor while lazy/cascade refinement continues in the same
    invocation. Cold-start semantic queries can show a bounded content
    preview from the anchor before refinement, metadata questions use a
    fast filesystem lane, and cross-folder diffusion is suppressed when
    the current scope already has a concrete anchor.
  - **`0.5.8.5`** — Multilingual intelligent routing hardening
    without weakening semantic recall. Natural-language CLI queries
    can now be passed bare, quoted, or smart-quoted, so
    `skygrep where is my case42 file in Downloads`,
    `skygrep -x where is my case42 file in Downloads`, and
    smart-quoted terminal input all route as one query instead of
    failing Click argument parsing. A generic cheap filename
    pre-router handles English, Chinese, and mixed-language file
    lookups (`我的 CASE42 文件在哪` -> `CASE42`, `我的合同文件在哪` ->
    `合同`) and a cheap semantic pre-router avoids cold LLM router
    latency for obvious `how` / `why` / `explain` questions. Filename
    answers can return immediately while background indexing continues;
    literal/rg evidence still feeds the normal cascade instead of
    suppressing semantic recall. Proactive outside-path diffusion is
    now bounded to filename intent, so wrong-folder file recovery stays
    fast without polluting ordinary semantic/code searches.
  - **`0.5.8.3`** — Hot-fix for high-confidence filename lookups
    that skipped the semantic cascade: the cascade branch initialised
    `queries`, but the cascade-skipped path did not, and the later
    warm cross-folder gate still read `len(queries)`. Queries like
    `skygrep -x "where is my case42 file"` could show proactive
    outside-project filename matches and then crash with
    `UnboundLocalError`. `queries` is now initialised before the
    branch; regression coverage locks the filename skip path.
  - **`0.5.8`** — **`--explain` / `-x`: why this matched.** Every
    retrieved chunk now carries the full retrieval provenance.
    Pass `--explain` and skygrep prints (a) a router rationale at
    the top — `├─ route      router: <intent> · primary_token=... · conf=... ·
    source=...` plus a one-sentence reason; (b) a per-result `via:`
    line — which channel(s) contributed (cosine cascade · symbol
    RRF · filename-lookup · ripgrep), what symbol terms matched,
    the score; and (c) a `├─ cascade   lane:` summary at the bottom
    with σ-adaptive evidence (`gap=... , tau=...`). Off by default —
    existing UX is byte-identical to 0.5.7. **Bonus:** if Ollama
    isn't running but is installed, skygrep autostarts it in the
    background and tells you. Two latent LLM-router bugs were also
    fixed (`keep_alive` coercion + `LLM_TIMEOUT_SECONDS` default
    bumped 0.5 s → 8 s) that had been silently forcing rule-based
    fallback on most queries. README + Pages now include a
    dedicated **"How skylakegrep differs from Elasticsearch"**
    section. 207 / 207 unit tests pass; head-to-head vs 0.5.7 PyPI
    on the same query produces byte-identical paths and scores
    when `--explain` is off.
  - **`0.5.7`** — Hot-fix for the cross-folder lazy worker:
    a SQLite cross-thread error was silently disabling the
    proactive lazy lane on wrong-path queries. Real-CLI receipt:
    first answer at ~1.1 s on a wrong-cwd query.
  - **`0.5.6`** — **Parallel proactive umbrella.** Cascade and
    `filename_extend` / `lazy_cwd` / `lazy_cross_folder` now all
    run at *t = 0* and stream the first confident answer to your
    terminal. Wrong-path queries that previously waited 99 s on a
    cascade rerank now answer in ~1 s.
  - **`0.5.3`** — **Cold-start lazy auto-trigger.** Vocabulary-
    mismatch hit-rate 0/10 → 4/10 over plain `rg` cold-start on
    the Django oracle bench, with no upfront `skygrep index .`
    ever run. Adds deterministic dir-token picker, numeric-prefix
    penalty, and import diffusion.
  - **`0.5.1`** — Lazy semantic auto-trigger on by default. The
    first query in any folder works without `skygrep index .`.
  - **`0.4.x → 0.5.0`** — Holistic graph-aware retrieval, then a
    rolled-back synthetic-only-bench misstep; 0.5.0 reset to
    real-CLI discipline as the only acceptable proof.
  - **`0.3.x`** — σ-adaptive cascade with Bayesian-evidence
    framing. Settled on `bge-m3` + cross-encoder rerank.
  - **`0.2.x`** — Multilingual `bge-m3` substrate, content-
    agnostic reference graph registry, 30 / 30 public-OSS recall
    (was 28 / 30).
  - **`0.1.0`** — Initial public release.

[Full release notes →](https://github.com/danielchen26/skylakegrep/releases)

---

## Project principles

Architecture rules every contributor (human or AI agent) should
follow. Recorded in
[`docs/principles.html`](docs/principles.html). Loaded into Claude
sessions automatically via `CLAUDE.md`.

  1. **Understanding > Enumeration** — substrate (LLM / embedder
     / registry) over hardcoded lists. Receipts table tracks 5
     past lapses.
  2. **Substrate before scaffolding** — upgrade the underlying
     model before layering priors on top.
  3. **Latency / quality / correctness** — in that priority order.
  4. **Public surfaces sync at every release** — the 8-surface
     checklist in [`docs/releasing.html`](docs/releasing.html).
  5. **Honest evaluation over hopeful claims** — name the bench,
     show the numbers, don't combine across benches.
  6. **Proactive over Passive** — when the cascade can't answer,
     try bounded extra work in parallel rather than shrug.

---

## Development

```bash
git clone https://github.com/danielchen26/skylakegrep.git
cd skylakegrep
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[rerank]

# Verify
.venv/bin/python -m pytest -q tests/        # current suite should pass
```

The release protocol is documented in
[`docs/releasing.html`](docs/releasing.html). Every release must
sync 8 public-facing surfaces (PyPI, GitHub Release, README,
GitHub Pages, plan docs, principles, version bump, tag) in a
specific order.

---

## License

PolyForm Noncommercial 1.0.0. Personal · academic · research ·
hobby use is fully permitted. Commercial use requires a separate
license — contact the maintainers.

---

## Acknowledgments

Built on the shoulders of:

  - [Ollama](https://ollama.com) — local model serving
  - [bge-m3](https://huggingface.co/BAAI/bge-m3) — multilingual
    embedder (BAAI)
  - [qwen2.5](https://huggingface.co/Qwen/Qwen2.5) — local LLM
    family for routing + answer synthesis
  - [tree-sitter](https://tree-sitter.github.io) — symbol-aware
    chunking
  - [SQLite](https://www.sqlite.org/) — durable index storage
  - [pypdf](https://pypdf.readthedocs.io) · [python-docx](https://python-docx.readthedocs.io) — binary content extraction
  - [Pygments](https://pygments.org) — syntax highlighting in the
    rendered terminal output
