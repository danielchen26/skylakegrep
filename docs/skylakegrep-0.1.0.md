# skylakegrep 0.1.0 — release notes

The first public release of `skylakegrep`. This is the objective
description of what the project is and does today, not a historical
incremental changelog.

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact the maintainers.

## What it does

`skygrep` is a local semantic code-search tool. It builds a SQLite
vector index from a repository's source files and answers
natural-language queries with line-cited snippets — fully offline,
backed by a local Ollama server.

```
$ skygrep "how does the auth token get refreshed?"

╭─ auth/refresh.py:34-51 ────────────────────────────  python   0.93
│ def refresh_token(refresh_jwt: str) -> AccessToken:
│     claims = jwt.decode(refresh_jwt, key, algorithms=ALGS)
│     ...
╰────────────────────────────────────────────────────────────────────

╭─ auth/middleware.py:78-94 ────────────────────────  python   0.81
│ async def renew_session(req: Request) -> Response:
│     if req.cookies.get("rt") and access_expired(req):
│     ...
╰────────────────────────────────────────────────────────────────────
```

## Capabilities

### LLM-driven query routing

A small local Ollama model (default `qwen2.5:3b`) reads each query
and returns a structured `{intent, primary_token, skip_cascade,
extract_content, confidence}` JSON. The CLI uses this to decide
which retrieval tiers to run.

Three-layer fallback chain:

```
primary    : LLM router (Ollama HTTP, 500 ms timeout)
                  ↓ on any failure
fallback-1 : hand-rolled rule-based classifier
                  ↓ on any failure
fallback-2 : intent="mixed" — every tier runs, no smart routing
```

Confidence threshold protects accuracy: `skip_cascade` is honored
only when LLM confidence ≥ 0.7. Below that, cascade always runs.
Per-session SQLite cache so the same query never pays the LLM cost
twice.

Override with `--no-llm-router` to force the rule-based path
(useful for benchmarking or air-gapped use).

### Three retrieval tiers, ranked by intent

```
L-2: filename lookup    ~10 ms   `where is X file?`
L-1: lexical content    ~50 ms   ripgrep-friendly content
L0+: semantic cascade   ~0.5-3s  vocabulary-mismatch
```

Each tier has a conservative four-condition gate; borderline queries
fall through. Every enabled tier always runs; results are
deduplicated by path and ranked by `classify_intent(query)` so the
right tier wins top slots.

### Confidence-gated semantic cascade

Sub-second cheap path (file-mean cosine) on most queries, escalating
to HyDE-augmented retrieval + cross-encoder rerank + PageRank
tiebreaker only on uncertain queries. Full pipeline:

  1. **L0 — ripgrep cold-start fallback.** When the index is being
     built in the background, the first query in a fresh project
     gets ripgrep results in ~100 ms.
  2. **L1 — file-mean cosine.** Whole-corpus cosine on file-level
     embeddings; cheap path returns when top-1 vs top-2 gap > τ.
  3. **L2 — symbol-aware retrieval.** Tree-sitter extracts
     function / class / method symbols; symbol matches are boosted.
  4. **L3 — HyDE + doc2query.** When the cheap path is uncertain,
     a local LLM rewrites the query into a hypothetical document
     for cosine retrieval over the union.
  5. **L4 — PageRank tiebreaker.** Per-file import / export graph
     produces a centrality prior that breaks score ties.
  6. **Cross-encoder rerank.** A second-stage rerank over the
     candidate pool for the highest-precision tier.

### Binary file content extraction

Lazy on-demand text extraction for filename matches in
`--detail=full`:

| Type | Extractor | Fallback |
| --- | --- | --- |
| `.pdf` (text layer) | `pdftotext` (poppler) | `pypdf` (pure Python) |
| `.pdf` (scanned) | `tesseract` (opt-in, `--ocr`) | hint to user |
| `.docx` | `python-docx` | error hint |
| `.txt`/`.md`/`.csv`/`.tsv` | direct read | n/a |

Scanned-PDF detection: text-layer extraction returning < 100
characters yields a friendly hint suggesting `--ocr`. OCR is
**never auto-run** (5–30 s/page is too slow for default).

Editor / app session-lock files (`~$*.docx` MS Word locks,
`*.swp` vim swap, `.#*` emacs lock, `*~` backup) are filtered at
both the find layer and the extract layer.

### Hierarchical merge with intent-aware ranking

Results from all tiers are deduplicated by path and ranked by
the LLM-detected intent. Filename-intent queries pin the
filename match to top while still surfacing relevant code chunks.
Semantic-intent queries put cascade results first and surface
filename matches only as low-priority context.

### Framed terminal output

Each result wrapped in a rounded card:

```
╭─ <path:lines> ──────────────────────────  <pill>   <score>
│ <symbol if present>
│
│ <Pygments-highlighted body>
╰────────────────────────────────────────────────────────────
```

- Cyan repo-relative paths
- Right-aligned cyan-pill language tag + bold-green score
- Pygments `Terminal256Formatter` (`monokai` default; override via
  `SKYGREP_PYGMENTS_STYLE`) — 300+ languages
- Dim grey separator rule
- Auto-detected: ANSI only when stdout is a TTY and `NO_COLOR`
  is unset; `SKYGREP_FORCE_COLOR=1` forces colour for piped output

### `--detail` levels

| Level | Body |
| --- | --- |
| `brief` | header only, no body |
| `standard` (default) | + ~10 lines of code body / metadata |
| `full` | + extracted PDF/docx content for filename matches |
| `summary` | + 1-line truncated preview |

### `skygrep setup` — auto-register with LLM CLIs

Detects installed coding agents (Claude Code, Codex, OpenCode,
Gemini CLI, Cursor) and writes a markdown snippet into each one's
user-level instructions file so the agent prefers `skygrep` over
`rg` for natural-language code questions. Snippet lives between
explicit BEGIN/END markers; `skygrep setup --uninstall` removes
it cleanly.

### Multi-language indexing

Tree-sitter grammars for **Python, JavaScript, TypeScript** are
bundled by default (declared in `pyproject.toml`). The indexer can
also use grammars for **Go, Rust, Java, C, C++, C#, Ruby, PHP,
Swift, Kotlin, Scala** if the corresponding `tree-sitter-<lang>`
package is installed manually (`pip install tree-sitter-rust` etc.).
Files in unsupported languages fall back to generic line-based
chunking.

`.gitignore` / `.skygrepignore` precedence honored.

### Per-project, fully offline

Per-project SQLite database at `~/.skylakegrep/repos/<basename>-<hash>.db`.
Indexes / embeddings / queries / answers all run on the local host
via Ollama. No remote service required. No telemetry.

### CLI flags

```
skygrep "<query>"                    # bare query (default subcommand)
skygrep search "<query>" -m 10       # explicit search subcommand
skygrep index                         # build / refresh the index
skygrep watch                         # incremental file watcher
skygrep stats                         # per-project index info
skygrep doctor                        # runtime + integrations health check
skygrep setup                         # register with LLM CLIs
skygrep enrich                        # one-time chunk doc2query enrichment
skygrep serve                         # daemon mode (avoids reranker cold-load)

Common flags:
  --top, -m N                          number of results
  --json                               machine-readable output
  --detail brief|standard|full|summary verbosity
  --ocr                                tesseract OCR for scanned PDFs (slow)
  --rg-shortcut/--no-rg-shortcut       lexical pre-gate (default on)
  --filename-shortcut/                 filename pre-gate (default on)
    --no-filename-shortcut
  --llm-router/--no-llm-router         LLM routing (default on)
  --cascade/--no-cascade               semantic cascade (default on)
  --rerank/--no-rerank                 cross-encoder rerank (default on)
  --hyde/--no-hyde                     HyDE rewrite
  --multi-resolution/                  two-stage file → chunk retrieval
    --no-multi-resolution
  --rank-by chunk|file                 ranking strategy
  --include / --exclude                path filters
  --language                           language filter
  --agentic                            local Ollama subquery decomposition
  --answer                             synthesise an answer from results
  --daemon-url                         use a running skygrep daemon
```

### Environment variables

```
OLLAMA_URL                  default http://localhost:11434
OLLAMA_EMBED_MODEL          default nomic-embed-text
OLLAMA_LLM_MODEL            default qwen2.5:3b
OLLAMA_HYDE_MODEL           default qwen2.5:3b
OLLAMA_KEEP_ALIVE           default -1 (keep models warm)

SKYGREP_DB_PATH             override per-project DB path
SKYGREP_FORCE_COLOR         force ANSI in non-TTY output
SKYGREP_LLM_ROUTER_MODEL    override LLM routing model
SKYGREP_LLM_ROUTER_TIMEOUT_SECONDS    default 0.5
SKYGREP_LLM_ROUTER_MIN_CONFIDENCE     default 0.7
SKYGREP_PYGMENTS_STYLE      default monokai
SKYGREP_AUTO_REFRESH_THROTTLE_SECONDS default 30
NO_COLOR                    standard ANSI opt-out
```

## Install

```bash
pip install skylakegrep
skygrep setup        # one-time: register with detected LLM CLIs
skygrep "where is the config file?"
```

Requires Python 3.9+ and a local Ollama server with at least one
embedding model pulled (`ollama pull nomic-embed-text`). Pull a
generation model only if you use `--answer`, `--agentic`, the LLM
router, or the cascade's HyDE escalation
(`ollama pull qwen2.5:3b`).

## Compatibility / honesty

  - 120 unit tests pass.
  - PolyForm Noncommercial 1.0.0 is **not** OSI-approved open source.
    GitHub will mark this repo as "non-permissive". Personal use
    remains fully free.
  - `pdftotext`, `tesseract`, `pdftoppm` are only required when
    using the corresponding extraction features; install via
    `brew install poppler tesseract` on macOS if needed.

## Why a fresh start?

This codebase has prior history — earlier iterations explored
cascade tuning, multi-language benchmarks, end-to-end agent
benchmarks, and a 5-tier retrieval architecture. The historical
incremental release notes are not preserved here because the
project has been re-licensed (MIT → PolyForm NC) and rebranded;
the current source tree, retroactively rewritten git history, and
this 0.1.0 release notes file are the single source of truth for
what `skylakegrep` is today.
