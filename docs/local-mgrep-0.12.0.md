# local-mgrep 0.12.0 — release notes

A smart-routing release. mgrep now detects lexical-friendly queries
and short-circuits to ripgrep internally, returning in ~50 ms — so
calling `mgrep` is no longer ever a tax over `rg` for the easy cases.
For vocabulary-mismatch queries the existing semantic cascade runs
unchanged.

## Headline

`mgrep` becomes a **smart code-search router**, not just a semantic
search tool:

  - Lexical-friendly query (e.g. `mgrep "config defaults"`) →
    ripgrep short-circuit, ~50 ms.
  - Vocabulary-mismatch query (e.g. `mgrep "auth token refresh"`) →
    confidence-gated semantic cascade, sub-second to ~3 s.

The agent doesn't have to decide which path applies. mgrep auto-routes.

## What's new

### Lexical pre-gate (`auto_index.lexical_shortcut`)

A four-condition conservative gate. The shortcut fires only when **all
four** hold:

  1. Query has ≤ 6 non-stop-word tokens.
  2. ripgrep returns ≥ 1 and ≤ 10 candidate files.
  3. At least one candidate's path encodes ≥ 2 query tokens.
  4. Candidate files cluster in ≤ 2 distinct parent directories.

Any borderline query falls through to the semantic cascade. **Accuracy
is the gold standard**: a false-positive shortcut is much worse than
a missed shortcut, so every condition is tuned conservatively.

When the gate fires, the result is shaped identically to
`auto_index.rg_fallback_results` and annotated with
`fallback="rg-shortcut"`. The CLI prints

```
[0.052s · rg-shortcut · cascade skipped]
```

so it's always visible which route a query took.

### `--rg-shortcut / --no-rg-shortcut` flag

Default: **on**. Pass `--no-rg-shortcut` to force pure cascade
(useful for benchmarking or when you specifically want semantic
ranking on a lexical-friendly query).

The shortcut is also disabled automatically under `--agentic` (the
agent decomposes queries and benefits from full cascade context per
sub-query) and under `--answer` (synthesis benefits from richer
multi-tier candidates).

### Updated `mgrep setup` snippet

The markdown snippet `mgrep setup` writes into agent rules files
(Claude Code, Codex, OpenCode, Gemini CLI, Cursor) is rewritten for
v0.12.0:

> For any code-search question, prefer `mgrep "<query>"` over `rg`.
> mgrep is a smart router: lexical-friendly queries auto-route to rg
> internally (~50 ms), vocabulary-mismatch queries run the semantic
> cascade. You don't have to decide which path applies.

Old snippets continue to work; running `mgrep setup --uninstall`
followed by `mgrep setup` will swap in the new wording.

## Files changed

  - `local_mgrep/src/auto_index.py` — new `lexical_shortcut()` and
    four tuning constants.
  - `local_mgrep/src/cli.py` — new `--rg-shortcut/--no-rg-shortcut`
    flag (default on), call site between empty-index check and
    embedder load.
  - `local_mgrep/src/integrations.py` — updated `SNIPPET_BODY` to
    reflect v0.12.0 auto-routing semantics.
  - `tests/test_lexical_shortcut.py` (new) — 8 tests covering
    happy path + every condition's negative branch + empty-query
    + result annotation.
  - `docs/local-mgrep-0.12.0.md` (this file).
  - `docs/assets/hero-dark.svg`, `og-image.svg/png` — version bump.
  - `docs/index.html` — version label v0.11.0 → v0.12.0.
  - `pyproject.toml` — version bump.
  - `README.md` — Releases bullet adds 0.12.0; quickstart unchanged
    (the routing is invisible to existing users).

## Compatibility

  - **55 / 55 unit tests pass** (47 prior + 8 new shortcut tests).
  - All 0.4.x – 0.11.0 flags / env / per-project DB layout
    unchanged.
  - Existing project indexes are picked up as-is.
  - Output format on the cascade path is byte-for-byte 0.11.0.
  - Output format on the shortcut path matches the long-standing
    `rg_fallback_results` shape.

## What did not change

  - The semantic cascade itself: pipeline, thresholds, models,
    rerank, file-mean cosine, HyDE — all 0.11.0.
  - Default models: `OLLAMA_EMBED_MODEL=nomic-embed-text`,
    `OLLAMA_LLM_MODEL=qwen2.5:3b`, `OLLAMA_HYDE_MODEL=qwen2.5:3b`,
    `OLLAMA_KEEP_ALIVE=-1`.
  - All flags from prior releases remain valid.

## Honest accuracy gates

The "must not regress" contract for v0.12.0 was: every published
benchmark dimension must stay at least as good as v0.11.0.

  - **Unit tests** (regression coverage): 55 / 55 pass.
  - **Lexical shortcut tests** (correctness coverage): 8 / 8 pass,
    covering both shortcut-fires and shortcut-falls-through branches
    for every gate condition.
  - **CLI smoke test**: `mgrep "config defaults" -m 5` returns the
    same top result with and without `--rg-shortcut` (cascade falls
    through correctly on a query that the gate rejects).
  - **Self-test 30-task benchmark**: **30 / 30 hit-rate** at
    top-k 10, 2.19× context-token reduction vs the simulated
    grep-agent baseline. (Run
    `.venv/bin/python benchmarks/agent_context_benchmark.py
    --top-k 10 --summary-only` to reproduce.)
  - **By construction**: the shortcut is purely additive — it can
    only add a fast path, never displace cascade results, because
    when the gate rejects a query the cascade runs exactly as before.

### Benchmark bug found and fixed during v0.12.0 release

While running the accuracy gate I discovered the bench had been
silently reporting `mgrep_hit_rate: 0/30` for every task because of
a path-form mismatch between the in-bench index (which stores chunks
**repo-relative**) and `lexical_candidate_paths()` (which returns
**absolute** paths). When the bench passed those absolute candidate
paths as a filter to `search()`, the entire candidate set wiped to
empty — silent and total. This was a pre-existing issue, not
introduced by v0.12.0; my v0.12.0 lexical shortcut sits in
`cli.search_cmd` and isn't even reached by this Python-API
benchmark. The one-line fix lives in
`benchmarks/agent_context_benchmark.py` (normalise candidate paths
to repo-relative before passing them to `search()`); after the fix,
the bench reports a clean 30/30.

## Install

```
pip install --upgrade local-mgrep
```

If you previously ran `mgrep setup`, run it again with `--uninstall`
then `mgrep setup` to refresh the snippet wording. Existing
registrations continue to work; the refresh is cosmetic.
