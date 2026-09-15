# skylakegrep roadmap

The current capabilities are documented in
[`skylakegrep-0.1.0.md`](skylakegrep-0.1.0.md). This file describes
what is planned beyond v0.1.0.

## Planned

### Agent search substrate hardening

The next architecture track is making `skygrep` a stronger
intermediate search tool for GPT, Claude Code, Codex, OpenCode, and
other LLM agents. The release benchmark now includes a synthetic
code + DOCX + PDF + metadata fixture so these changes can be tested
without any private files or machine-local examples.

1. **Explicit search-depth contract.** Add a first-class depth model:
   `path`, `anchor`, `excerpt`, `evidence`, `answer`, and `auto`.
   Path-depth queries can stop at file location; semantic and agent
   queries must continue until they have query-relevant evidence or a
   clear limitation. This keeps fast cases fast without returning an
   anchor when the caller needs content.

2. **Agent-first JSON schema.** Extend `--json` with structured routing
   and quality fields: `intent`, `depth_used`, `routing_path`,
   `index_state`, `confidence`, `is_anchor_only`,
   `semantic_depth_satisfied`, `evidence[]`, `limitations[]`, and
   `next_suggested_query`. Agents should not need to parse human
   terminal text to decide whether they have enough information.

3. **Metadata reliability.** Strengthen "latest / opened / modified /
   largest" queries by separating filesystem fallbacks from platform
   signals. On macOS, explore Spotlight / recent-document metadata as an
   optional source; always expose `metadata_source` and confidence so
   atime-based answers are not overstated.

4. **PDF / DOCX evidence hardening.** Cache extracted text, attach page
   or paragraph provenance when available, detect scanned PDFs, and keep
   OCR opt-in with explicit budgets. Agent JSON should include raw
   query-focused excerpts plus enough source metadata to cite them.

5. **Progress / streaming for long searches.** Add a machine-readable
   progress mode such as `--trace-jsonl`: router decision, preliminary
   filename/rg hits, semantic cheap pass, lazy/cross-folder progress,
   timeout, and final quality state. Human CLI should never sit silent;
   agent callers should be able to stream status without scraping stderr.

6. **Scoped cross-folder control.** Add explicit scope controls:
   `--scope cwd|project|configured|home`, `--no-cross-folder`, and
   `--cross-folder-budget-ms`. Cross-folder expansion remains useful for
   wrong-directory recovery, but agent benchmarks and automation need
   deterministic search boundaries.

7. **Release-gated agent benchmark.** Promote the synthetic benchmark to
   a checked-in release gate covering exact-symbol code, vocabulary
   mismatch code, filename path lookup, semantic file anchors, DOCX
   excerpts, PDF excerpts, metadata, answer handoff, cold index, stale
   index, and partial index recovery. Every release must publish the
   pass/fail table and timings.

8. **Quality scoring and sufficiency.** Each result should state whether
   it is only an anchor, whether raw excerpts were found, how strong the
   evidence is, and whether deeper search is recommended. This is the
   guardrail that prevents agents from treating a plausible path as a
   complete answer.

### Native MCP server — shipped

`skygrep mcp` serves `search`, `index`, and `stats` as Model Context
Protocol tools over stdio, with declared input and output schemas,
`structuredContent` results, and degraded-retrieval reported as
`warnings` rather than an empty result set. `skygrep setup` (markdown
snippets in agent rules files) stays for agents that have no MCP client.

Still open here: registry listings, and an HTTP transport for clients
that cannot spawn a local process.

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

`skygrep serve` now accepts requests before any optional reranker
warmup, and `--warm-reranker` can amortise that load in the
background. It still does not pool embedder/reranker model instances
optimally for concurrent requests; production-grade concurrent
scheduling remains future work.

## Not on the roadmap

- **Cloud-hosted index.** This project is local-first by design.
- **Removing the Ollama dependency.** `skygrep` is intentionally
  built on top of Ollama for local LLM access. Other backends
  (e.g. llama.cpp direct) are possible but not prioritised.
- **Charging for the core.** As of the relicense to Apache-2.0,
  commercial use of skylakegrep itself is free and stays free. Support
  with an SLA, a shared on-premises team index, and integration work are
  the paid things; the search tool is not.
