# skylakegrep roadmap

The current capabilities are documented in
[`skylakegrep-0.1.0.md`](skylakegrep-0.1.0.md). This file describes
what is planned beyond v0.1.0.

## Planned

### Native MCP server

`skygrep setup` writes a markdown snippet into agent rules files
telling the agent to prefer `skygrep` over `rg`. A native MCP
server (Model Context Protocol) would let agents call `skygrep` as
a structured tool instead of a shell command — better schema,
fewer parsing failures, cleaner error surfaces.

### Multi-vector / late-interaction retrieval (ColBERT-style)

The current cascade uses single-vector cosine + cross-encoder
rerank. Token-level late interaction (ColBERT-style) over a larger
candidate pool is the most promising lever for the residual ~5 %
miss rate on hand-labelled multi-language benchmarks. Costs ~10×
index size; needs hardware-aware tuning.

### Larger benchmark task sets

Current published numbers are based on 20 single-turn + 1 multi-
turn agent task. Push n into the 100+ range to move statistical
significance from "directional" to "tight". Real-user query
corpora are the goal.

### Packaged benchmark fixtures

Third parties should be able to reproduce every published number
without copying internal scripts. Needs a `benchmarks/` CLI plus
fixture repos pinned to specific commits.

### Expanded language coverage

Tree-sitter grammars and chunk heuristics for Go, Java, Kotlin,
Swift, Rust (currently relies on generic chunking for Rust). Plus
richer `.gitignore` / `.skygrepignore` precedence rules.

### Daemon mode hardening

`skygrep serve` exists but does not pool embedder/reranker model
instances optimally for concurrent requests. A production-grade
daemon would amortise model warm-load across all queries on the
host.

## Not on the roadmap

- **Cloud-hosted index.** This project is local-first by design.
- **Removing the Ollama dependency.** `skygrep` is intentionally
  built on top of Ollama for local LLM access. Other backends
  (e.g. llama.cpp direct) are possible but not prioritised.
- **MIT relicensing.** This project is PolyForm Noncommercial
  1.0.0 and stays that way. Commercial users should contact for
  a commercial license.
