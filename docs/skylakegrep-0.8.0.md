# skylakegrep 0.8.0 — Token-lean agent loop

0.8.0 makes the agent path cheaper without giving up completion. Agent
presets now return compact JSON, a new `--agent-slim` preset and
`skygrep symbols` command let an agent read only the line ranges that
matter, and an indexing bug that could leave a project silently without
embeddings is fixed.

## Why

Measured against an agent that truncates tool output, the 0.7.x agent loop
did not save tokens. In the 2026-08-15 General Benchmark v2 receipt:

- 77% of `skygrep-first` tokens were spent after the correct file was
  already in context.
- 70% went to one step that read the symbol inventories of 50–160 files at
  once (median ~60k tokens).
- The `rg-only` baseline let `--max-count` apply per file, so common terms
  returned most of a repository (median 13.8M tokens per Spring task). The
  18× headline therefore measures savings against an unbounded dump.

## What changed

- **Compact agent JSON.** `--agent-fast`, `--agent-context` and the MCP
  `agent_context` tool return single-line JSON with paths relative to the
  current directory, no duplicated `evidence_bundle` / `why_ranked` /
  `supporting_chunks` blocks, and snippets trimmed to the declaration plus
  the lines that mention the query (`--snippet-budget`, default 1,200
  characters). The keys agents read are unchanged: `path`, `start_line`,
  `end_line`, `score`, `confidence`, `evidence_terms`, `agent_summary`,
  `strict_verification`. On the same Django query, `--agent-fast` went from
  3,331 to 508 tokens and `--agent-context` from 9,297 to 2,700.
- **`--format auto|compact|full`.** Agent presets default to compact; plain
  `--json` and `--format full` keep the 0.7.x shape.
- **`--agent-slim`.** `--agent-fast` retrieval plus a query-ranked
  `line: declaration` outline of the top 3 source files and a `next` command
  for the following batch.
- **`skygrep symbols FILE... -q QUERY`.** Model-free outline of a few files:
  imports skipped, plain lines only when they mention two or more query
  concepts, `--budget` per file, opt-in `--expand K` to inline the first 30
  lines of the best-matching declarations. Also exposed as the MCP tool
  `symbols`.
- **MCP tool text is no longer pretty-printed.** The text block is what the
  model reads; indentation was pure overhead.
- **Agent guidance v6.** `skygrep setup` teaches the progressive loop
  (slim → read the range → `symbols` on the next batch → follow-up probe)
  and the managed snippet shrinks from about 1,660 to about 560 tokens per
  agent session. Existing registrations refresh automatically.
- **Index fix: chunks embedded while the model was unreachable.** The
  embedder substitutes zero vectors when Ollama is down, and they used to be
  stored with the file's real mtime, so neither `skygrep index .` nor the
  per-search refresh re-embedded them and `doctor` reported the index as
  healthy. They are now stored with an mtime sentinel, staleness checks use
  the minimum chunk mtime, and `doctor` reports unembedded chunks with the
  repair command.
- **Benchmark policies.** `skygrep-slim` (the progressive loop, with the
  full `skygrep-first` policy as a last-resort fallback) and `rg-agent` (a
  realistic ripgrep agent: per-term `rg -c` capped at 250 visible lines,
  BM25-style ranking, the same outline reader, reads capped at 2,000 lines),
  plus `benchmarks/compare_policies.py` for paired comparisons.

## Compatibility

- **Agent JSON shape changed.** Tools that parsed `evidence_bundle`,
  `why_ranked`, `language`, `candidate_recall_lanes` or absolute paths from
  `--agent-fast` / `--agent-context` should pass `--format full`.
- Still Python 3.9+, Apache-2.0, fully offline. Existing indexes keep
  working; chunks stored without embeddings are re-embedded on the next
  `skygrep index .` once Ollama is reachable.

## Measurements

A smoke run on the 60 pinned General Benchmark v2 tasks (1 trial, chars/4
token counting). The run environment could not download `bge-m3`, so
embeddings came from a deterministic stand-in served on the Ollama API: the
semantic lane is noise for both skygrep policies. This is a regression check,
not a published receipt.

| policy | gate passed | median tokens | total tokens | median calls |
|---|---|---|---|---|
| skygrep-first | 60/60 | 52,751 | 3,474,615 | 14 |
| skygrep-slim | 60/60 | 8,358 | 1,425,866 | 10 |
| rg-agent | 46/60 | 14,778 | 1,587,936 | 8 |

- `skygrep-slim` matched `skygrep-first` completion with a 4.4× lower median
  and 2.4× lower total.
- Against `rg-agent` it completed 14 more tasks. On the 46 both complete,
  its median was 1.31× lower, but its total was higher (ratio 0.75×) because
  the 5 tasks that fall back to the full policy dominate the sum.

## Known follow-ups

- Publish a `bge-m3` receipt for `skygrep-slim` / `rg-agent` with three
  trials and `--tokenizer tiktoken` under `benchmarks/reports/`.
- Make the last-resort fallback progressive end to end so the 5 fallback
  tasks stop dominating total tokens.
- Revisit the README hero numbers ("30 / 30" recall, "~1 s" warm queries)
  against the current receipt.
