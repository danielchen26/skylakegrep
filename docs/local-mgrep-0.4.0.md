# local-mgrep 0.4.0 — release notes

A focused UX revision: the CLI should feel like the cloud product it
emulates. After ``pip install local-mgrep`` you should be able to ``mgrep
"<question>"`` from any project directory, with the index, model, and
runtime concerns happening behind the scenes (or producing actionable
errors when they cannot).

## Headline changes

### Bare-form invocation

``mgrep "<query>"`` now works as a first-class command. The first non-flag
token routes through the search subcommand automatically. Subcommand
names (``index``, ``search``, ``watch``, ``serve``, ``stats``,
``doctor``) still take precedence — quote a query that collides with a
subcommand to disambiguate (``mgrep "stats and metrics"``).

```
mgrep "where is the auth token refreshed?"
```

### Per-project auto-index

Indexes are now scoped per project, derived from
``git rev-parse --show-toplevel`` (falling back to the working
directory). Each project gets a stable file under
``~/.local-mgrep/repos/<basename>-<8hex>.db``. The first query against a
fresh project runs a full index with a progress line:

```
⏳ Indexing 3,247 files in /path/to/project (one-time setup) …
  · 200/3247 files · 4,210 chunks · 21.4s
✓ Indexed 38,902 chunks across 3,247 files in 124.3s
```

Subsequent queries do an mtime-based incremental refresh:

```
↻ refreshed 4 file(s).
```

A 30-second throttle prevents back-to-back queries from re-paying the
mtime scan; tune via ``MGREP_AUTO_REFRESH_THROTTLE_SECONDS``.

If you set ``MGREP_DB_PATH`` explicitly the auto-index policy switches
to off (curated indexes are not auto-mutated). Pass
``--no-auto-index`` to opt out for a single search.

### Cascade is the default

The confidence-gated cascade introduced in 0.3.0 is now the default
search path. Reverting to chunk-only retrieval is one flag:

```
mgrep "..." --no-cascade --rerank   # chunk-cosine + cross-encoder rerank
mgrep "..." --no-cascade --no-rerank # raw cosine + lexical blend
```

### Default embedding model is ``nomic-embed-text``

Switched from ``mxbai-embed-large`` to ``nomic-embed-text`` (already
present in Ollama's recommended set, supports asymmetric
``search_query: ``/``search_document: `` prefixes, modestly better recall
on code). Indexes built under ``mxbai-embed-large`` (1024-d) trigger a
dimension-mismatch warning at search time with a one-line migration:

```
mgrep index . --reset
```

### ``mgrep doctor``

A new health-check command summarises every dependency and the project
index in one place:

```
mgrep doctor
  Ollama runtime            ✓ http://localhost:11434
  Embed model               ✓ nomic-embed-text
  Llm model                 ✓ qwen2.5:3b
  Project index             ✓ 3,247 files / 38,902 chunks · refreshed 12 min ago
  Index DB                  /Users/.../local-mgrep-49a9be2e.db
  Reranker (optional)       ✓ sentence-transformers installed
  Project root              /Users/.../my-project
```

### Status line

Every search now ends with a status line covering retrieval cost,
cascade decision, and index freshness:

```
[1.487s · cascade=cheap (gap=0.0241 τ=0.0150) · index 12 min ago · 3247 files]
```

### Bootstrap (``bootstrap.py``)

The CLI no longer fails opaquely when the Ollama runtime or a model is
missing. ``mgrep doctor`` and the auto-index path probe the runtime and
print actionable text:

```
Ollama not reachable at http://localhost:11434: connection refused

Ollama is required for local embeddings. Install on macOS:
    brew install ollama
or follow https://ollama.com/download. After install, start the server:
    ollama serve  &
```

Models can be pulled on demand: pass ``MGREP_AUTO_PULL=yes`` to skip the
``y/N`` prompt and run ``ollama pull <model>`` inline.

## Breaking changes (and how to migrate)

This is a deliberate UX overhaul. Two flags changed defaults:

  - ``--cascade`` flipped from off → on. Pass ``--no-cascade`` for the
    pre-0.4.0 chunk-only path.
  - ``OLLAMA_EMBED_MODEL`` default flipped from ``mxbai-embed-large`` →
    ``nomic-embed-text``. Existing indexes built under the old model
    keep working with ``OLLAMA_EMBED_MODEL=mxbai-embed-large``; or run
    ``mgrep index . --reset`` once to migrate.

The default DB location moved from a single global file
(``~/.local-mgrep/index.db``) to per-project files
(``~/.local-mgrep/repos/<basename>-<8hex>.db``). To keep using a single
global DB, set ``MGREP_DB_PATH=~/.local-mgrep/index.db`` (or any path
of your choosing) explicitly. ``mgrep doctor`` will report whichever
DB you are currently using.

## What changed under the hood

  - **New** ``local_mgrep/src/bootstrap.py`` — Ollama / model probes,
    optional auto-pull, ``doctor`` report builder.
  - **New** ``local_mgrep/src/auto_index.py`` — first-time index with
    progress, mtime-based incremental refresh with throttle, status
    helpers (``index_status``, ``index_age_human``).
  - **Refactor** ``local_mgrep/src/cli.py`` — custom ``MgrepCLI`` group
    that routes unknown first-args to ``search``; new ``doctor``
    subcommand; status line; expanded ``stats`` output.
  - **Refactor** ``local_mgrep/src/config.py`` — new
    ``project_root()`` / ``project_db_path()`` / ``resolve_db_path()``
    helpers; per-project DB derivation from git toplevel.
  - **Tests** ``tests/test_v0_4_ux.py`` — bare-form routing,
    project-root detection, DB-path policy, doctor report.

## Compatibility

  - Existing scripts using ``mgrep search "<query>"`` keep working.
  - Existing scripts using ``mgrep index <path>`` keep working.
  - Daemon / serve / watch are unchanged.
  - All previous CLI flags remain functional. New defaults flipped on
    ``--cascade`` and ``OLLAMA_EMBED_MODEL`` (see Breaking changes).
  - 24 / 24 unit tests pass.

## Install

```
pip install --upgrade local-mgrep
```
