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
any of 100+ languages) and get the right file + line range in
under a second.

```console
$ skygrep "where does the auth token get refreshed?"

═══ auth/middleware.py:78-94          score 0.91 · python
async def renew_session(req: Request):
    # swap the access cookie when the refresh JWT is still valid
    if req.cookies.get("rt") and access_expired(req):
        return await refresh_token(claims, key)

[0.5s · path=cosine-cheap · σ-gap=0.082 ≥ τ=0.005 (adaptive) → high-confidence early-exit · ✓ quality=BEST]
```

[**Install in 30 s →**](#install) &nbsp;·&nbsp;
[How it works →](#how-it-works) &nbsp;·&nbsp;
[Benchmarks →](#performance)

> **30 / 30** public-OSS recall (fully-indexed) &nbsp;·&nbsp;
> **+30 %** lazy auto-trigger over `rg` cold-start (0.5.3) &nbsp;·&nbsp;
> **~1.1 s** first answer on wrong-path queries via parallel proactive umbrella (0.5.7, real-CLI verified) &nbsp;·&nbsp;
> **~1 s** warm queries &nbsp;·&nbsp;
> **100 %** local &nbsp;·&nbsp;
> **38** releases shipped

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

> No `rg` hit for *"session refresh"*; cosine bridges to `renew_session` in 0.5 s.

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

> **~1.1 s wall** on a wrong cwd, real-CLI verified (0.5.6 / 0.5.7).

---

### 🧠 Streaming intelligent routing

Each query is classified by a local LLM router (`qwen2.5:3b`) for
intent / scope / primary token, then dispatched to multiple lanes
in parallel. Each result lands tagged with the route it came from
and the still-searching status of the others — never silent, always
honest about what's pending.

```console
$ skygrep "the design doc on rate limiter rewrite"

▾ proactive umbrella · filename glob          [‖ cascade still searching]
═══ docs/rate-limiter-redesign.md:1

[0.4 s · router → mixed intent · 2 lanes dispatched]
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

🧭 router: filename · primary_token="pyproject.toml" · conf=0.95 · source=llm
   reason: "user is looking for a specific file by name in the repo"

╭─ pyproject.toml ────────────────────────────────── [toml]  1.000
│ via: filename-lookup · token "pyproject.toml" · score=1.000
│
│ size:  1.0 KB    modified: 2026-05-06 16:51    type: toml
╰──────────────────────────────────────────────────────────────────

🛤  cascade lane: cosine-cheap (gap=0.037, tau=0.016)
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


### Reading the per-query telemetry footer (0.2.2+)

Every search prints a one-line footer so you can see *which*
retrieval path answered your query and *why*:

```
✓ 0.42s · quality=BEST
   path     : cosine-cheap (high-confidence early-exit)
   router   : llm → intent=mixed (0.83)
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

<p align="center">
  <img alt="skylakegrep — environment variable configuration grouped into Ollama setup, Indexing &amp; rerank, and Behavior toggles" src="docs/assets/configuration.svg" width="100%">
</p>


---

## Release history

**Recent releases** (in reverse chronological order):

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
    the top — `🧭 router: <intent> · primary_token=… · conf=… ·
    source=…` plus a one-sentence reason; (b) a per-result `via:`
    line — which channel(s) contributed (cosine cascade · symbol
    RRF · filename-lookup · ripgrep), what symbol terms matched,
    the score; and (c) a `🛤 cascade lane:` summary at the bottom
    with σ-adaptive evidence (`gap=… , tau=…`). Off by default —
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
.venv/bin/python -m pytest -q tests/        # 201 / 201 should pass
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
