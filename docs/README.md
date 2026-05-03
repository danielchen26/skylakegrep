# Documentation

This directory contains the documentation site for `local-mgrep`. The
[`index.html`](index.html) file is the rendered site published to GitHub Pages.
The Markdown files here are reference companions to that site.

## Contents

| File | Purpose |
| --- | --- |
| [`index.html`](index.html) | Rendered documentation site (published as <https://danielchen26.github.io/local-mgrep/>). |
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
