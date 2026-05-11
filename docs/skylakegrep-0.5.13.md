# skylakegrep 0.5.13 — adaptive candidate recall for agent-grade context

0.5.13 upgrades the retrieval architecture behind semantic and mixed
queries. The router still decides intent, but it no longer gets to be
the only gate deciding which files are visible. A bounded candidate
recall substrate now runs before the expensive cascade so agent calls get
better path coverage and more source evidence without dumping a whole
repository into the downstream LLM context.

All examples and benchmark tasks below are generic repository-maintenance
scenarios. No private prompts, filenames, folders, screenshots, or local
paths are included.

## What changed

- Added a generic candidate recall lane for semantic, lexical, and mixed
  queries. It combines independent, bounded signals from explicit
  `--include` scopes, indexed path tokens, indexed symbols, SQLite chunk
  text, and a small `rg -il -F` path pass.
- Candidate recall is additive evidence, not a hard shortcut. It can
  surface likely files early, but the normal semantic scorer and reranker
  still decide final ordering.
- `--content` and agent calls now receive a small per-file support pack
  when needed: related constants, symbol definitions, or assertion
  anchors from the same recalled file. `brief` / location output stays
  compact; `standard` gets compact support; `full` can read wider after
  the caller narrows scope.
- The indexer now preserves meaningful module-level text, constants, and
  long string anchors that tree-sitter structural chunking can otherwise
  miss. This improves evidence coverage for agent-instruction files,
  tests, configuration modules, and documentation-like source files.
- Candidate recall now participates in intent-aware merge priority:
  semantic and mixed queries can trust it as a peer evidence lane, while
  pure filename queries still prefer exact filename lookup first.
- The public README, GitHub Pages homepage, benchmark page, CLI
  reference, JSON reference, and setup instructions now explain the
  information-depth ladder for LLM callers: bare lookup, `--content`,
  `--detail full`, `--answer`, `--json`, and `--include`.

## Why it matters for LLM agents

Raw `rg` is a strong recall ceiling, but it gives an agent too much
unranked text. 0.5.13 is optimized for the context that a coding agent
actually consumes between reasoning steps:

| Agent need | Recommended command |
| --- | --- |
| Locate the file or concept | `skygrep "where is the project brief?"` |
| Inspect snippets for the next step | `skygrep --content --detail standard "what does the migration plan say about rollback?"` |
| Read deeply after narrowing scope | `skygrep --content --detail full --include "docs/migration-plan.md" "show the deployment steps"` |
| Ask for a synthesized local answer | `skygrep --answer --content "summarize the retry policy"` |
| Feed structured agent context | `skygrep --json --content --detail standard --include "src/**" "where is token refresh implemented?"` |

The important rule is: use the smallest depth that can answer the next
reasoning step. Agents should add `--include` whenever they already know
the relevant repo, folder, or file; scoped calls are faster and reduce
irrelevant cross-folder evidence.

## Agent tool-context benchmark

The release gate runs `benchmarks/agent_tool_depth_benchmark.py`, a local
deterministic benchmark that simulates two tool-use policies:

- `skygrep-agent`: one structured `skygrep --json --content` call per
  task.
- `rg-agent`: multiple raw `rg` term searches per task, then line-window
  context.

The score is context-centric: path coverage, path precision, evidence
term coverage, tool-call budget, context-token budget, and latency. It
does not call remote Claude, GPT, or any cloud model.

Current 0.5.13 release-gate totals:

| Metric | `skygrep-agent` | raw `rg-agent` | Interpretation |
| --- | ---: | ---: | --- |
| Tasks × effort profiles | 24 | 24 | 8 generic tasks across low / medium / high depth |
| Path coverage | 81.9 % | 100.0 % | `rg` remains the recall ceiling |
| Path precision | 34.9 % | 12.2 % | skygrep returns much less irrelevant path noise |
| Evidence coverage | 79.2 % | 92.7 % | support packs close much of the evidence gap |
| Sufficiency score | 80.8 % | 97.1 % | weighted 60 % path + 40 % evidence |
| Tool calls | 24 | 147 | skygrep used 6.12× fewer tool calls |
| Context tokens | 56,424 | 2,129,655 | skygrep emitted 37.74× less context |
| Sufficiency per 1k tokens | 0.344 | 0.011 | skygrep was 31.27× denser context |

The honest reading: raw `rg` is still faster at producing a large dump,
and it is the recall ceiling when an agent can afford to inspect a huge
context blob. 0.5.13 is stronger when the downstream model needs compact,
ranked, sufficient context rather than an unfiltered repository slice.

## Before vs after

| Query shape | Previous behavior | 0.5.13 behavior |
| --- | --- | --- |
| Semantic query whose answer is in a source constant or module-level instruction string | Structural chunking could miss the anchor | supplemental text chunks preserve it in the index |
| Agent asks for source evidence, not just a path | Top chunk could identify the file but omit nearby proof | bounded support pack adds related same-file anchors |
| Router classifies a query as semantic or mixed but cheap lexical/path evidence knows a likely file | Cascade alone could miss or delay that file | candidate recall adds it to the candidate pool before final ranking |
| Agent already knows the scope | Scope could help filtering but not always recall | explicit include recall votes scoped files into the pool |
| Human location query | Could receive too much snippet support | location-depth output stays compact; support packs are tied to content depth |

## Verification

- Full local test suite: required before upload.
- Targeted regression coverage: candidate recall lanes, lexical evidence
  selection, support-pack attachment, module-level text supplementation,
  include-scope recall, merge priority, and terminal UI layout.
- Agent tool-context benchmark: required before upload, with the current
  totals above.
- Source privacy scan: required before build.
- Wheel/sdist privacy scan: required before upload.
- Fresh install and local editable install: required after PyPI upload.

## Compatibility

- No CLI flag was removed.
- JSON fields remain backwards compatible. Candidate-recall metadata is
  additive and optional.
- Existing indexes are compatible, but rebuilding the index lets 0.5.13
  capture the new supplemental module-level text chunks.
- Existing `skygrep setup` managed snippets auto-refresh inside their
  managed BEGIN/END block only; user-authored instructions outside that
  block are preserved.

## Privacy note

This release uses only generic public examples and repository-maintenance
benchmark tasks. The release process blocks on source and distribution
privacy scans so no real user prompt, private filename, private folder
name, absolute local machine path, screenshot, document category, name,
or email is published.
