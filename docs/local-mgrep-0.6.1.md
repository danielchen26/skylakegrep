# local-mgrep 0.6.1 — release notes

A correctness patch on top of 0.6.0. Three fixes, no new architecture.

## What changed

### Bug fix: ``keep_alive=-1`` was sent as a string and rejected by Ollama

0.6.0 introduced the ``OLLAMA_KEEP_ALIVE`` config and threaded it
through every embed and generate request. The default value
``-1`` (keep models resident indefinitely) was passed verbatim
as the JSON string ``"-1"``, which Ollama's HTTP API rejects with
``400 Bad Request: invalid duration "-1"``. The result was that
both the embedder and the answerer fell back to zero-vectors and
empty strings on every cascade-escalation, which made repo-A
benchmark recall and latency meaningless.

0.6.1 adds ``answerer._coerce_keep_alive`` to convert numeric
strings to integers before sending. Pass-through still accepts
duration strings (``"5m"``, ``"1h"``); pure numbers are coerced
to int (``"-1"`` → ``-1``, ``"60"`` → ``60``); empty / ``None``
omits the key. This restores the keep-alive semantics 0.6.0
intended.

### HyDE default reverted to ``qwen2.5:3b`` after measurement

0.6.0 set the default HyDE model to ``qwen2.5:1.5b`` on the theory
that "write a plausible code snippet" is a small-model-friendly
job and the 3-5× speedup justified using it. After the
``keep_alive`` bug was fixed and a clean benchmark could be run,
the smaller model **lost 1 task on repo-A 16-task recall**:

| Config | Recall | Avg s/q |
| --- | :-: | :-: |
| qwen2.5:3b + keep_alive=-1 | **16 / 16** | 2.0–3.7 s (noisy) |
| qwen2.5:1.5b + keep_alive=-1 | 15 / 16 | 2.0 s |

The miss is task 12 (``app/src/command_palette.rs``, "Where is the
keyboard-shortcut driven command palette opened and ranked?"). The
1.5 B model produces less plausible identifiers for niche concepts
like keystroke handlers and command-palette UI; the 3 B model
remains the safer default.

The ``qwen2.5:1.5b`` config is still supported and useful when a
user wants the speed and accepts the recall trade. Set explicitly:

```bash
export OLLAMA_HYDE_MODEL=qwen2.5:1.5b
```

The CLI surface (the ``OLLAMA_HYDE_MODEL`` env, the graceful
fallback when the configured model isn't installed, the
``mgrep doctor`` row) is unchanged.

### Tag-aware model presence check

``mgrep doctor`` reported ``qwen2.5:1.5b ✓`` even when only
``qwen2.5:3b`` was installed locally, because the presence check
matched on base name. This was a real false positive: a HyDE call
to the missing tag would 404 and silently fall back to
``llm_model``, but doctor told the user everything was fine.

0.6.1 makes the check tag-aware:

  - ``qwen2.5:1.5b`` requires an exact ``qwen2.5:1.5b`` install.
  - ``nomic-embed-text`` (no tag) still matches both
    ``nomic-embed-text`` and ``nomic-embed-text:latest``, since
    Ollama treats those as the same model.
  - ``qwen2.5:3b`` no longer matches ``qwen2.5:1.5b``.

## Compatibility

  - 40 / 40 unit tests pass.
  - Existing project indexes are picked up as-is.
  - All 0.5.x / 0.6.0 flags remain valid.
  - Recall on repo-A 16-task with default config: **16 / 16** (back
    to 0.5.1's empirically-validated baseline).

## Install

```
pip install --upgrade local-mgrep
```
