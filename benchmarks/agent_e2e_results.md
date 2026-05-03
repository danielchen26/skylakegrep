# End-to-end Claude Code agent benchmark — raw results (v0.8.0)

Methodology: 6 questions × 2 conditions = 12 sub-agents spawned in parallel via the Claude Code `Agent` tool. Each agent answered the same question with the same model under one of:
- **rg-only**: prompt forbade `mgrep`; allowed `rg`, `find`, `ls`, `head`, `cat`, `Read`, `Grep`.
- **mgrep-on**: prompt instructed `mgrep` as primary tool; `Read` allowed for verification; `rg`/`grep`/`find` forbidden.

Each agent returned a JSON `{file, lines, evidence}` answer. Token / tool-call / wall-time totals from each sub-agent's own usage telemetry.

| # | Repo | Question | Expected | rg-only · tok / tools / time / correct | mgrep · tok / tools / time / correct |
|---|---|---|---|---|---|
| A | <repo-D> (Rust) | microphone audio for STT | `crates/voice_input/` | 35,219 / 6 / 29 s / ✓ | 35,279 / 4 / 18 s / ✓ |
| B | <repo-D> (Rust) | websocket reconnect | `crates/websocket/` | 32,757 / 10 / 36 s / ✗* | 43,580 / 5 / 31 s / ✗* |
| C | <repo-A> (Python) | root CLI entry | `<module-x>/cli.py` | 30,060 / 10 / 37 s / ✓ | 36,436 / 8 / 154 s / ✓ |
| D | <repo-A> (Python) | finite-field event partitioning | `<module-x>/finite_field_runner.py` | 30,346 / 7 / 30 s / ✓ | 28,451 / 1 / 117 s / ✓ |
| E | <repo-B> (TypeScript) | vim motions dispatch | `src/vim/motions.ts` | 28,757 / 7 / 27 s / ✓ | 28,641 / 1 / 19 s / ✓ |
| F | <repo-B> (TypeScript) | MCP server client | `src/services/mcp/client.ts` | 37,264 / 6 / 22 s / ✗ | 29,539 / 2 / 24 s / ✓ |

\*Task B (<repo-D> websocket): both agents converged on `app/src/.../listener.rs` or `app/src/.../viewer/network.rs` — files where the actual reconnect-after-drop logic lives, not the `crates/websocket/` crate primitive. Same labelling artefact as <repo-D> tasks 0 / 14 documented in 0.5.1 release notes. Generous label expansion: both pass; strict label: both fail. We report strict.

## Aggregate

|  | rg-only | mgrep-on | Δ (mgrep − rg) |
|---|:-:|:-:|:-:|
| **Tokens (sum, 6 tasks)** | 194,403 | 201,926 | +3.9 % (mgrep slightly more) |
| **Tool calls (sum)** | 46 | 21 | **−54 %** |
| **Tool calls (avg/task)** | 7.7 | 3.5 | **−54 %** |
| **Wall time (sum)** | 181 s | 363 s | +101 % (confounded — see below) |
| **Strict-label correct** | 4 / 6 | 5 / 6 | **+1 task** |
| **Lenient-label correct** | 5 / 6 | 6 / 6 | **+1 task** |

## What the data actually shows

  - **Tool-call reduction is real and large** (−54 %). mgrep returns ranked semantic candidates so the agent stops needing 6-10 separate `rg` / `Read` / `head` calls to triangulate the right file. With mgrep the agent often makes 1-2 tool calls and reads exactly one file. This translates directly into less context bloat in the agent's reasoning loop.
  - **Token consumption is roughly equal** (+3.9 %). Tool calls drop, but the per-tool-call payload (mgrep's snippet + score) is not dramatically smaller than `rg`'s file-list + a couple of `Read`s on the agent's side once the candidates have been narrowed. The agent's own reasoning tokens dominate.
  - **Wall time looks worse for mgrep here, but is confounded** by the benchmark methodology: 6 mgrep-on agents were spawned in parallel against the same Ollama instance, so the cascade-escalation HyDE + embed calls queued behind each other. <repo-A> tasks especially show this (mgrep wall times of 117 s and 154 s vs ~30 s rg-only). In normal usage one user runs one mgrep at a time and Ollama is not contended; in the v0.6.x small-project demo, warm queries land in 0.1-0.5 s. **Treat the wall-time row as not-clean** for the parallel-bench artefact.
  - **Quality is slightly better with mgrep** (+1 task strict, +1 task lenient). mgrep's semantic ranking found the canonical `services/mcp/client.ts` directly on task F where rg-only's path-token search picked a sibling file (`useManageMCPConnections.ts`).

## Caveats

1. n = 6 tasks. Statistical significance is weak; this is a measurement, not a study.
2. Both conditions used the same underlying model (general-purpose sub-agent). A future bench could vary the model and prompt to test sensitivity.
3. The agent was *told* not to use the disallowed tool. We did not enforce at the API level. Tool-list audits in each result confirm compliance: rg-only agents made 6-10 tool calls of `rg/find/ls/head/Read`; mgrep-on agents made 1-8 tool calls dominated by `mgrep`/`Read`. No agent violated its constraint.
4. Tasks were drawn from existing per-repo `*.json` benchmarks (2 per repo × 3 repos). Easy and clear-canonical questions; harder questions in the original 40-task set might widen the gap.

## Headline (cautious)

**On real Claude Code agent runs over 6 hand-labelled questions in 3 languages, mgrep cuts agent tool-call count by 54 % and improves answer correctness by 1/6, at roughly equivalent token cost.** Wall-time data was contaminated by parallel-spawn Ollama contention and is not reportable without a fresh sequential run.

The earlier "17.7× total-token reduction" claim is from a *deterministic simulated* grep-agent (`benchmarks/agent_context_benchmark.py`) and measures static retrieval-output volume, not an agent's reasoning loop. Both numbers are valid for different questions; this section is the more realistic one for "what does mgrep save in a real Claude Code session".
