# local-mgrep 0.6.0 — release notes

A latency-focused release. Average per-query latency on Mac CPU drops
substantially without changing recall on the repo-A 16-task benchmark
(still 16/16 with corrected labels). Two simple but compounding
changes:

  - **Cascade escalation now uses a smaller LLM by default.** New
    ``OLLAMA_HYDE_MODEL`` env var; default ``qwen2.5:1.5b``. The HyDE
    job (write a short plausible code snippet matching the question's
    intent) does not need a 3B model — a 1.5B model handles it
    competently and 3-5× faster per call on Mac CPU. The
    ``--answer`` and ``--agentic`` paths keep using
    ``OLLAMA_LLM_MODEL`` (default ``qwen2.5:3b``) for higher-quality
    long-form output.
  - **Ollama keep-alive defaults to ``-1`` (resident indefinitely).**
    Both the embedder and the answerer pass ``keep_alive`` in every
    Ollama API request. The first cascade escalation in a shell
    session pays the cold-load; every subsequent query in the same
    session hits a warm model. Override with
    ``OLLAMA_KEEP_ALIVE=30m`` or ``=0`` to opt out.

Net effect: average repo-A 16-task latency on Mac CPU **3 s/q → ~1 s/q
band** (workload-dependent — cheap-path queries stay 0.1-0.3 s, only
the ~20% that escalate benefit). First query in a shell session is
slower by the LLM cold-load (~5-10 s for the smaller model), every
subsequent query within the keep-alive window pays only inference
time (~1 s for HyDE on qwen2.5:1.5b vs ~3-5 s on qwen2.5:3b).

## What you can do

```bash
pip install --upgrade local-mgrep

# Pull the new default HyDE model once. Fallback to OLLAMA_LLM_MODEL
# is automatic if you skip this — the CLI prints a one-time hint.
ollama pull qwen2.5:1.5b

mgrep "..."
mgrep "..."
mgrep "..."   # second+ query hits warm Ollama, no cold-load

mgrep doctor  # now reports HyDE model + keep-alive setting
```

`mgrep doctor` output gains two pieces of information:

```
mgrep doctor
  Ollama runtime            ✓ http://localhost:11434
  Embed model               ✓ nomic-embed-text
  Llm (--answer) model      ✓ qwen2.5:3b
  Llm (cascade/HyDE) model  ✓ qwen2.5:1.5b           ← NEW
  Ollama keep_alive         -1                        ← NEW
  Project index             ✓ 247 files / 3812 chunks · refreshed 2 min ago
  Enriched chunks           0 / 3812 (0.0%)
  ...
```

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| ``OLLAMA_HYDE_MODEL`` | ``qwen2.5:1.5b`` (NEW) | Model used for cascade-escalation HyDE generation. Falls back to ``OLLAMA_LLM_MODEL`` automatically when the configured model is not installed locally. |
| ``OLLAMA_KEEP_ALIVE`` | ``-1`` (NEW) | Passed through to every Ollama generate / embed call. ``-1`` keeps the model resident indefinitely after first load. ``"30m"`` / ``"60s"`` / ``"0"`` accepted. |
| ``OLLAMA_LLM_MODEL`` | ``qwen2.5:3b`` | Used by ``--answer`` (longer, higher-quality synthesis) and ``--agentic`` (subquery decomposition). Unchanged. |
| ``OLLAMA_EMBED_MODEL`` | ``nomic-embed-text`` | Unchanged. |

The graceful fallback means existing users with only ``qwen2.5:3b``
installed see no behavioural change beyond a single one-time
``stderr`` hint suggesting they pull the smaller model. The CLI does
not block on the fallback path.

## What changed under the hood

  - ``local_mgrep/src/config.py`` — adds ``DEFAULT_HYDE_MODEL``,
    ``DEFAULT_KEEP_ALIVE`` constants and surfaces ``hyde_model``,
    ``keep_alive`` from ``get_config()``.
  - ``local_mgrep/src/answerer.py`` — ``OllamaAnswerer`` now takes
    ``hyde_model`` and ``keep_alive`` kwargs. ``hyde()`` routes to the
    smaller model with auto-fallback to ``self.model`` when the
    smaller model is not installed (404 from Ollama). ``decompose()``
    and ``answer()`` stay on ``self.model`` for quality. All three
    methods now pass ``keep_alive`` in their API payload.
  - ``local_mgrep/src/embeddings.py`` — ``OllamaEmbedder`` accepts
    ``keep_alive`` and threads it through the single-embed and batch-
    embed payloads.
  - ``local_mgrep/src/bootstrap.py`` — ``doctor_report`` reports the
    HyDE model as a separate row (when distinct from
    ``llm_model``) and surfaces the ``keep_alive`` setting in the
    structured payload.
  - ``local_mgrep/src/cli.py`` — ``mgrep doctor`` renders the
    ``Ollama keep_alive`` line.

## Compatibility

  - ``OllamaAnswerer(base_url, model)`` still works (existing tests
    construct it that way). New kwargs default to ``None`` and route
    through the model parameter when not provided.
  - 40 / 40 unit tests pass. No test changes required for this
    release — backward-compatible at every public surface.
  - Existing project indexes are picked up as-is.
  - All 0.5.x flags remain valid.
  - Downgrade path: set ``OLLAMA_HYDE_MODEL=$OLLAMA_LLM_MODEL`` and
    ``OLLAMA_KEEP_ALIVE=`` to restore exact 0.5.1 behaviour.

## What did not change

  - The cascade architecture is unchanged. L0 / L1 / L2 / L3 / L4
    layers all behave the same. Only the LLM models they invoke and
    the ``keep_alive`` they pass are different.
  - Recall on repo-A 16-task: still 16/16 with corrected labels.
  - The ``--rerank`` cross-encoder path is untouched.
  - L3 doc2query enrichment remains opt-in via ``mgrep enrich`` and
    is honestly documented as not yet validated to move recall on
    any benchmark we can run.

## Install

```
pip install --upgrade local-mgrep
ollama pull qwen2.5:1.5b   # optional but recommended for full speed-up
```
