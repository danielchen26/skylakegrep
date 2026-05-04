# Documentation

This directory contains the documentation site for `local-mgrep`. The
[`index.html`](index.html) file is the rendered site published to GitHub Pages.
The Markdown files here are reference companions to that site.

## Contents

| File | Purpose |
| --- | --- |
| [`index.html`](index.html) | Rendered documentation site (published as <https://danielchen26.github.io/local-mgrep/>). |
| [`local-mgrep-0.12.1.md`](local-mgrep-0.12.1.md) | Release notes for 0.12.1: terminal output rework — cyan repo-relative paths, right-aligned language pill + bold-green score, dim separator rule, lightweight ANSI syntax highlighting. Visually aligned with the landing-page hero. `--json` and pipe/redirect behaviour unchanged; `NO_COLOR=1` opt-out honoured. |
| [`local-mgrep-0.12.0.md`](local-mgrep-0.12.0.md) | Release notes for 0.12.0: smart-routing release. A four-condition conservative lexical pre-gate short-circuits ripgrep-friendly queries (~50 ms) so calling `mgrep` is no longer ever a tax over `rg` for the easy cases. Vocabulary-mismatch queries still run the full semantic cascade. New `--rg-shortcut/--no-rg-shortcut` flag (default on). |
| [`local-mgrep-0.11.0.md`](local-mgrep-0.11.0.md) | Release notes for 0.11.0: `mgrep setup` interactive command auto-registers local-mgrep as preferred semantic search with **Claude Code, Codex, OpenCode, Gemini CLI, and Cursor**. First-run banner nudges new users; `mgrep setup --uninstall` removes all snippets cleanly. |
| [`local-mgrep-0.10.0.md`](local-mgrep-0.10.0.md) | Release notes for 0.10.0: multi-turn benchmark + 6-task expansion. Headline: **−82 % tool calls in multi-turn sessions, −37.6 % across 20 single-turn tasks**. On 5 / 6 medium tasks, mgrep finishes in 1 tool call. |
| [`local-mgrep-0.9.0.md`](local-mgrep-0.9.0.md) | Release notes for 0.9.0: e2e Claude Code agent benchmark extended with 8 hard semantic questions (14 total). Headline: **−30 % agent tool calls + 2 / 14 better answers** with mgrep across Rust + Python + TypeScript. Best-case 25 × fewer tool calls on the hardest semantic query; worst-case mgrep slightly worse on lexical-friendly questions. Honest worst-case publishing. |
| [`local-mgrep-0.8.0.md`](local-mgrep-0.8.0.md) | Release notes for 0.8.0: end-to-end Claude Code agent benchmark (mgrep on/off across 6 questions × 3 languages). Headline: **−54 % agent tool calls** with mgrep, +1 task in answer correctness, equal token cost. (Superseded by 0.9.0 with larger sample.) |
| [`local-mgrep-0.7.0.md`](local-mgrep-0.7.0.md) | Release notes for 0.7.0: multi-language benchmark (Rust + Python + TypeScript, 40 hand-labelled questions across 3 repos). Headline: **38 / 40 (95 %) recall** at 3.55 s/q on Mac CPU. |
| [`local-mgrep-0.6.2.md`](local-mgrep-0.6.2.md) | Release notes for 0.6.2: Ollama preheat (fire-and-forget warm-up at search start), GitHub Actions CI workflows (pytest + auto-PyPI on tag), and a 1200×630 social preview card wired into ``og:image`` meta. No retrieval architecture change. |
| [`local-mgrep-0.6.1.md`](local-mgrep-0.6.1.md) | Release notes for 0.6.1: ``keep_alive=-1`` correctness fix; HyDE default reverted to ``qwen2.5:3b`` after measurement showed ``qwen2.5:1.5b`` cost 1 task; tag-aware model presence check. |
| [`local-mgrep-0.6.0.md`](local-mgrep-0.6.0.md) | Release notes for 0.6.0: introduced ``OLLAMA_HYDE_MODEL`` env and Ollama ``keep_alive`` plumbing. Superseded by 0.6.1 for default correctness. |
| [`local-mgrep-0.5.1.md`](local-mgrep-0.5.1.md) | Release notes for 0.5.1: cascade file-mean cosine corpus-wide fix; repo-A 16-task benchmark relabeled to acceptable-alternatives form (16/16 with corrected labels); honest empirical note that L2/L3/L4 don't move repo-A recall (repo-A saturated, multi-language bench landing in 0.5.2). |
| [`local-mgrep-0.5.0.md`](local-mgrep-0.5.0.md) | Release notes for 0.5.0: 5-layer progressive system — symbol-aware indexing (L2), doc2query enrichment (L3), PageRank tiebreaker (L4) on top of the 0.4.x rg fallback + cascade base. |
| [`local-mgrep-0.4.1.md`](local-mgrep-0.4.1.md) | Release notes for 0.4.1: ripgrep fallback for the first query in a fresh project (~0.7 s) + detached background semantic indexer. |
| [`local-mgrep-0.4.0.md`](local-mgrep-0.4.0.md) | Release notes for 0.4.0: bare-form `mgrep "<query>"`, per-project auto-index, `mgrep doctor`, cascade default, default embed model `nomic-embed-text`. |
| [`local-mgrep-0.3.1.md`](local-mgrep-0.3.1.md) | Release notes for 0.3.1: README + diagram refresh so the PyPI page reflects the cascade flow. No behaviour change. |
| [`local-mgrep-0.3.0.md`](local-mgrep-0.3.0.md) | Release notes for 0.3.0: confidence-gated cascade (`--cascade`), benchmark sweep, null-result findings. |
| [`local-mgrep-0.2.0.md`](local-mgrep-0.2.0.md) | Capability guide for the 0.2.0 release: indexing, ranking, output modes, configuration. |
| [`token-benchmarking.md`](token-benchmarking.md) | Methodology and full results for the deterministic context-gathering benchmark. |
| [`assets/`](assets) | SVG figures referenced by the site and the project README. |

## Reading order

1. The [project README](../README.md) for installation and a one-page summary.
2. [`local-mgrep-0.3.0.md`](local-mgrep-0.3.0.md) for the latest release
   notes (`--cascade` retrieval, benchmark deltas, null-result findings).
3. [`local-mgrep-0.2.0.md`](local-mgrep-0.2.0.md) for what the 0.2.0 release
   implements and how each component is invoked.
4. [`token-benchmarking.md`](token-benchmarking.md) for the benchmark protocol,
   limitations, and the conditions under which the published numbers are valid.

## Architecture at a glance

![local-mgrep system architecture](assets/architecture.svg)

`local-mgrep` is organized as two pipelines that meet at a single SQLite
database. The index pipeline (`mgrep index`, `mgrep watch`) discovers source
files, chunks them, embeds them through a local Ollama server, and writes the
result to disk. The query pipeline (`mgrep search`) reads from the same
database, scores candidates with a hybrid cosine + lexical formula, applies
span deduplication and per-file diversification, and returns the top-k as text,
JSON, or a synthesized answer.

## Reproducing the published benchmark

```bash
.venv/bin/python benchmarks/agent_context_benchmark.py --top-k 10 --summary-only
```

See [`token-benchmarking.md`](token-benchmarking.md) for definitions, the full
results table, and an explicit list of what the benchmark does not measure.
