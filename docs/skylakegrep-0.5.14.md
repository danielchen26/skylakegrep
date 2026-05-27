# skylakegrep 0.5.14 — closed-loop agent workflow and daemon-first guidance

0.5.14 turns the agent-use strategy into an explicit public contract.
The core retrieval engine remains compatible with 0.5.13, but the CLI
help, setup snippets, README, GitHub Pages, and benchmarks now teach a
stricter path-first / evidence-second workflow for LLM callers.

All examples and benchmark tasks below are generic repository-maintenance
scenarios. No private prompts, filenames, folders, screenshots, local
paths, or user-specific document categories are included.

## What changed

- Agent instructions now separate path discovery from evidence
  gathering. Location questions should use
  `skygrep --json --no-content --top 10 --no-rerank "<query>"`; first
  source-evidence passes should use
  `skygrep --json --content --detail standard --no-rerank "<query>"`.
- Repeated LLM tool calls are documented as daemon-first:
  start `skygrep serve --port 7878`, then call
  `skygrep --daemon-url http://127.0.0.1:7878 --json ...` so agents do
  not pay fresh process and model-warmup cost for every query.
- JSON path-only output now obeys `--no-content` strictly. When an
  agent asks for paths only, the result omits `snippet` instead of
  leaking hidden text into what should be a compact anchor contract.
- Public setup snippets for Claude Code, Codex, OpenCode, Gemini CLI,
  and Cursor now include the same option playbook and closed-loop policy:
  scope early with `--include`, read returned files directly when the
  agent has a file-read tool, use `--detail full` only after narrowing,
  and reserve rerank for ambiguous final evidence passes.
- The benchmark suite now includes a universal closed-loop benchmark that
  measures path coverage, evidence coverage, sufficiency, tool-call
  count, context tokens, raw retrieval latency, estimated downstream
  agent latency, and work quality per minute.

## What "agent fast" and "daemon-first" mean

These are operating modes for how agents call skygrep; they are not a
new separate router or a hardcoded query preset.

| Mode | Meaning | Command shape |
| --- | --- | --- |
| Agent fast path discovery | Return likely files with minimal context so the next tool can read the right files directly. | `skygrep --json --no-content --top 10 --no-rerank "where is token refresh implemented?"` |
| Agent fast evidence | Return bounded snippets and line ranges for the next reasoning step, without expensive rerank on the first pass. | `skygrep --json --content --detail standard --no-rerank "what does the retry policy say about backoff?"` |
| Scoped deep read | After a path is known, use skygrep extraction only where it adds value, such as PDFs, DOCX, or unavailable direct file reads. | `skygrep --json --content --detail full --include "docs/retry-policy.md" "show the rollout steps"` |
| Daemon-first repeated calls | Keep the process warm for multi-step agent sessions. | `skygrep serve --port 7878` plus `skygrep --daemon-url http://127.0.0.1:7878 --json ...` |

The principle is generic: minimize the first context packet, preserve
source evidence, and deepen only when the next reasoning step actually
needs more text.

## Closed-loop agent benchmark

The 0.5.14 release gate runs
`benchmarks/universal_closed_loop_benchmark.py` against 38 generic tasks
across skylakegrep, Django, React, and Tokio. It compares:

- `skygrep-first`: path-only JSON probes, compact snippet probes,
  scoped includes when known, direct reads after narrowing, and
  `--no-rerank` on first-pass agent calls.
- `rg-only`: repeated raw `rg` term searches and line-window reads.

Current 0.5.14 totals:

| Metric | `skygrep-first` | raw `rg-only` | Interpretation |
| --- | ---: | ---: | --- |
| Tasks | 38 | 38 | self + Django + React + Tokio |
| Path coverage | 94.7 % | 100.0 % | `rg` remains the recall ceiling |
| Path precision | 10.9 % | 3.4 % | skygrep returns less path noise |
| Evidence coverage | 99.1 % | 99.3 % | near-parity evidence coverage |
| Sufficiency score | 96.5 % | 99.7 % | weighted path + evidence score |
| Completed tasks | 35 | 38 | remaining misses are explicit follow-ups |
| Tool calls | 322 | 337 | similar call count, much smaller payloads |
| Raw retrieval elapsed | 154.23 s | 327.73 s | 2.12× lower measured retrieval time |
| Estimated agent elapsed | 161.68 s | 3833.97 s | 23.71× lower after context-read cost |
| Context tokens | 223,592 | 105,187,419 | 470× less context for skygrep |
| Work quality / minute | 12.829 | 0.561 | 22.87× higher closed-loop utility rate |

Honest framing: raw `rg` is still the recall ceiling, and this benchmark
keeps the three skygrep misses visible. The 0.5.14 win is not "skygrep
always beats grep"; it is that the skygrep-first workflow gives agents
nearly the same usable evidence while avoiding the enormous context load
that makes downstream LLM reasoning slow and noisy.

## Compatibility

- No existing CLI flag was removed.
- Existing indexes remain compatible.
- JSON remains backwards compatible for normal content calls. The
  `--json --no-content` path intentionally omits `snippet`, matching the
  caller's request for path-only output.
- Existing managed setup snippets refresh inside their managed block when
  `skygrep setup` runs, and already-registered snippets are refreshed by
  normal `skygrep` searches / `skygrep doctor` after upgrade.

## Verification

- Targeted tests: daemon JSON path-only output, setup snippet refresh,
  and closed-loop benchmark helpers.
- Compile check over changed Python entrypoints, benchmark scripts, and
  tests.
- Universal closed-loop benchmark over 38 generic tasks.
- Source privacy scan before build.
- Wheel/sdist build and `twine check` before upload.
- Wheel/sdist privacy scan before upload.

## Known follow-ups

- Consider a future explicit CLI convenience layer, such as
  `--agent-fast path` / `--agent-fast context`, once the command shapes
  have stayed stable across more public projects.
- Improve the remaining ambiguous implementation-location tasks where
  raw `rg` still finds a path that the skygrep-first policy misses.
- Keep expanding the benchmark corpus before promoting these workflow
  shortcuts to a larger 0.6 line.
