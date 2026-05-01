# local-mgrep Documentation

<p align="center">
  <img src="assets/architecture.svg" alt="local-mgrep local-first architecture" width="100%">
</p>

<h3 align="center">The public documentation hub for local-first semantic code search.</h3>

This is the professional documentation entry point for `local-mgrep`. Start here
when you want to install it, understand what it can do, verify the benchmark
claims, or integrate it into a local coding-agent workflow.

<table>
  <tr>
    <td width="50%">
      <h3>🚀 Start here</h3>
      <p>Install Ollama, install the CLI, index a repository, and run your first semantic search.</p>
      <p><a href="../README.md#installation"><strong>Open installation guide →</strong></a></p>
    </td>
    <td width="50%">
      <h3>⚙️ Capability Guide</h3>
      <p>See every implemented feature: indexing, watch mode, hybrid ranking, JSON, answer mode, and agentic search.</p>
      <p><a href="local-mgrep-0.2.0.md"><strong>Open capability guide →</strong></a></p>
    </td>
  </tr>
  <tr>
    <td width="50%">
      <h3>📊 Benchmark Report</h3>
      <p>Review the deterministic token benchmark, methodology, top-k tradeoffs, and limitations.</p>
      <p><a href="token-benchmarking.md"><strong>Open benchmark report →</strong></a></p>
    </td>
    <td width="50%">
      <h3>🏗️ Architecture</h3>
      <p>Understand the local pipeline: source files → ignore rules → chunking → Ollama embeddings → SQLite vectors → ranked context.</p>
      <p><a href="local-mgrep-0.2.0.md#search-behavior"><strong>Open architecture notes →</strong></a></p>
    </td>
  </tr>
</table>

## Quick navigation

| Topic | Link |
| --- | --- |
| Project landing page | [`../README.md`](../README.md) |
| Installation | [`../README.md#installation`](../README.md#installation) |
| CLI reference | [`../README.md#cli-reference`](../README.md#cli-reference) |
| Capability matrix | [`../README.md#capability-matrix`](../README.md#capability-matrix) |
| 0.2.0 capability guide | [`local-mgrep-0.2.0.md`](local-mgrep-0.2.0.md) |
| Token benchmarking | [`token-benchmarking.md`](token-benchmarking.md) |

## Current headline benchmark

At top-k 10 on the deterministic repository navigation benchmark:

```text
mgrep hit rate:                       30/30
grep hit rate:                        30/30
estimated total-token reduction:      2.00x
context-token reduction:              2.90x
```

This is a local deterministic benchmark, not hosted provider billing data. See
[`token-benchmarking.md`](token-benchmarking.md) for the exact protocol.

## Local-first promise

`local-mgrep` keeps the core search workflow on your workstation:

- local source scanning,
- local Ollama embeddings,
- local SQLite vector storage,
- local ranking and result diversification,
- optional local answer synthesis.

No hosted account, cloud index, source upload, or paid model API is required for
the core workflow.
