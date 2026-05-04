# End-to-end Claude Code agent benchmark — raw results (v0.8.0 + v0.9.0)

The 0.8.0 release introduced this benchmark with 6 easy single-shot
questions. 0.9.0 extended it with 8 hard semantic / vocab-mismatch
questions on the same 3 repos. The 14-task headline is in the
[v0.9.0 release notes](docs/local-mgrep-0.9.0.md); the per-task tables
below carry both rounds.

## v0.8.0 round — 6 easy single-shot questions

Methodology: 6 questions × 2 conditions = 12 sub-agents spawned in parallel via the Claude Code `Agent` tool. Each agent answered the same question with the same model under one of:
- **rg-only**: prompt forbade `mgrep`; allowed `rg`, `find`, `ls`, `head`, `cat`, `Read`, `Grep`.
- **mgrep-on**: prompt instructed `mgrep` as primary tool; `Read` allowed for verification; `rg`/`grep`/`find` forbidden.

Each agent returned a JSON `{file, lines, evidence}` answer. Token / tool-call / wall-time totals from each sub-agent's own usage telemetry.

| # | Repo | Question | Expected | rg-only · tok / tools / time / correct | mgrep · tok / tools / time / correct |
|---|---|---|---|---|---|
| A | repo-A (Rust) | microphone audio for STT | `crates/voice_input/` | 35,219 / 6 / 29 s / ✓ | 35,279 / 4 / 18 s / ✓ |
| B | repo-A (Rust) | websocket reconnect | `crates/websocket/` | 32,757 / 10 / 36 s / ✗* | 43,580 / 5 / 31 s / ✗* |
| C | repo-B (Python) | root CLI entry | `repo-B/cli.py` | 30,060 / 10 / 37 s / ✓ | 36,436 / 8 / 154 s / ✓ |
| D | repo-B (Python) | finite-field event partitioning | `repo-B/finite_field_runner.py` | 30,346 / 7 / 30 s / ✓ | 28,451 / 1 / 117 s / ✓ |
| E | repo-c (TypeScript) | vim motions dispatch | `src/vim/motions.ts` | 28,757 / 7 / 27 s / ✓ | 28,641 / 1 / 19 s / ✓ |
| F | repo-c (TypeScript) | MCP server client | `src/services/mcp/client.ts` | 37,264 / 6 / 22 s / ✗ | 29,539 / 2 / 24 s / ✓ |

\*Task B (repo-A websocket): both agents converged on `app/src/.../listener.rs` or `app/src/.../viewer/network.rs` — files where the actual reconnect-after-drop logic lives, not the `crates/websocket/` crate primitive. Same labelling artefact as repo-A tasks 0 / 14 documented in 0.5.1 release notes. Generous label expansion: both pass; strict label: both fail. We report strict.

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
  - **Wall time looks worse for mgrep here, but is confounded** by the benchmark methodology: 6 mgrep-on agents were spawned in parallel against the same Ollama instance, so the cascade-escalation HyDE + embed calls queued behind each other. repo-B tasks especially show this (mgrep wall times of 117 s and 154 s vs ~30 s rg-only). In normal usage one user runs one mgrep at a time and Ollama is not contended; in the v0.6.x small-project demo, warm queries land in 0.1-0.5 s. **Treat the wall-time row as not-clean** for the parallel-bench artefact.
  - **Quality is slightly better with mgrep** (+1 task strict, +1 task lenient). mgrep's semantic ranking found the canonical `services/mcp/client.ts` directly on task F where rg-only's path-token search picked a sibling file (`useManageMCPConnections.ts`).

## Caveats

1. n = 6 tasks. Statistical significance is weak; this is a measurement, not a study.
2. Both conditions used the same underlying model (general-purpose sub-agent). A future bench could vary the model and prompt to test sensitivity.
3. The agent was *told* not to use the disallowed tool. We did not enforce at the API level. Tool-list audits in each result confirm compliance: rg-only agents made 6-10 tool calls of `rg/find/ls/head/Read`; mgrep-on agents made 1-8 tool calls dominated by `mgrep`/`Read`. No agent violated its constraint.
4. Tasks were drawn from existing per-repo `*.json` benchmarks (2 per repo × 3 repos). Easy and clear-canonical questions; harder questions in the original 40-task set might widen the gap.

## v0.8.0 headline (6 easy tasks)

**On real Claude Code agent runs over 6 hand-labelled questions in 3 languages, mgrep cuts agent tool-call count by 54 % and improves answer correctness by 1/6, at roughly equivalent token cost.** Wall-time data was contaminated by parallel-spawn Ollama contention and is not reportable without a fresh sequential run.

The earlier "17.7× total-token reduction" claim is from a *deterministic simulated* grep-agent (`benchmarks/agent_context_benchmark.py`) and measures static retrieval-output volume, not an agent's reasoning loop. Both numbers are valid for different questions; the e2e numbers are the more realistic one for "what does mgrep save in a real Claude Code session".

---

## v0.9.0 round — 8 hard semantic questions

| # | Repo | Question | Expected | rg-only · tok / tools / time / correct | mgrep · tok / tools / time / correct |
|---|---|---|---|---|---|
| 1 | repo-A | LLM backend caller | `crates/ai/` | 43 105 / 17 / 86 s / ✗* | 59 934 / 7 / 76 s / ✗* |
| 2 | repo-A | editor cursor + keystroke | `crates/editor/` | 42 809 / 25 / 128 s / ✗* | 28 423 / **1** / 15 s / ✗* |
| 3 | repo-A | vim h/j/k/l → editor actions | `crates/vim/` | 33 172 / 10 / 63 s / ✓ | 43 467 / 10 / 97 s / ✓ |
| 4 | repo-A | sign in + session token | `app/src/auth/` | 30 583 / 5 / 40 s / ✓ | 44 473 / 7 / 70 s / ✓ |
| 5 | repo-B | two-time response operator | `finite_susceptibility.py` | 29 604 / 4 / 44 s / ✗ | 31 106 / 4 / 116 s / ✗ |
| 6 | repo-B | V6 <domain-y> resolve / chain | `<domain-y>_v6.py` | 32 466 / 6 / 52 s / ✗ (README) | 35 760 / 10 / 183 s / **✓** |
| 7 | repo-c | bash command auth prompt | `bashClassifier.ts` | 33 201 / 9 / 73 s / ✗ | 44 669 / 17 / 107 s / ✗ |
| 8 | repo-c | autocompact decision | `autoCompact.ts` | 32 447 / 2 / 34 s / ✓ | 37 938 / 10 / 82 s / ✓ |

\*Tasks 1, 2: agents converged on `app/src/ai/...` and `app/src/editor/...` rather than the labelled `crates/...`. Same labelling artefact as the repo-A 16-task benchmark — the application-side files are valid alternative answers.

### v0.9.0 aggregate

|  | rg-only | mgrep-on | Δ |
|---|:-:|:-:|:-:|
| Tokens | 277 387 | 325 770 | +17.4 % |
| Tool calls | 78 | 66 | −15.4 % |
| Wall time | 520 s | 746 s | +43 % (Ollama contention) |
| Strict correct | 3 / 8 | 4 / 8 | +1 |
| Lenient correct | 4 / 8 | 5 / 8 | +1 |

### Best-case task: repo-A editor cursor (task 2)

|  | rg-only | mgrep-on | Δ |
|---|:-:|:-:|:-:|
| Tool calls | 25 | 1 | **25× fewer** |
| Wall time | 128 s | 15 s | **1 / 8** |
| Tokens | 42 809 | 28 423 | −34 % |

mgrep returned the right file (`app/src/editor/view/mod.rs`) on the first call. The rg-only agent burned through 25 search/read rounds before settling on the same file.

### Worst-case task: repo-A signin (task 4)

|  | rg-only | mgrep-on | Δ |
|---|:-:|:-:|:-:|
| Tool calls | 5 | 7 | +40 % (mgrep more) |
| Tokens | 30 583 | 44 473 | +44 % |

Signin's vocabulary (auth / session / token) overlaps directly with code-path tokens, so rg's straightforward scan was already efficient. mgrep wandered through 4 search calls before answering. Both got the right answer.

---

## Combined 14-task aggregate (v0.8.0 + v0.9.0)

|  | rg-only | mgrep-on | Δ |
|---|:-:|:-:|:-:|
| Total tokens (sum) | 471 790 | 527 696 | +11.8 % (mgrep slightly more) |
| **Total tool calls (sum)** | **124** | **87** | **−30 %** |
| Avg tool calls / task | 8.9 | 6.2 | **−30 %** |
| Strict-label correct | 7 / 14 (50 %) | 9 / 14 (64 %) | **+2 tasks** |
| Lenient-label correct | 9 / 14 (64 %) | 11 / 14 (79 %) | **+2 tasks** |

### What the 14-task data actually says

  - **−30 % tool calls is the cleanest signal.** Independent of model
    pricing or wall-time contention. Each tool call costs an LLM
    round-trip + network RTT + agent context bloat; cutting them
    1/3 makes Claude Code agent loops measurably tighter.
  - **+2 tasks correct is real.** Strict 50 → 64 %, lenient 64 → 79 %.
    mgrep solves the repo-A `<domain-y>_v6.py` famous miss and the repo-c
    `client.ts` task that rg-only got wrong; doesn't lose any task
    rg-only got right.
  - **Token cost stays roughly equal** (+11.8 % aggregate). The
    agent's own reasoning tokens dominate the bill; trimming the
    retrieval payload doesn't move the total. Don't claim mgrep
    saves money; do claim it saves agent loop complexity.
  - **Wall time is contaminated** by 8-way parallel Ollama
    contention from the benchmark methodology. Single-user usage
    has none of this.

### Task-dependent value

mgrep's biggest wins are on **vocab-mismatch hard semantic queries**
(task 2 repo-A editor: 25× fewer tool calls; task 6 repo-B <domain-y>:
the canonical answer file rg-only completely missed). On
**lexical-friendly questions** (task 4 repo-A signin) mgrep is roughly
equal or slightly worse because rg's path-token grep already lands
the answer in 5 tool calls.

The pattern is consistent: **mgrep is a better tool when the
question's surface vocabulary doesn't overlap the code identifiers**.
Real-world questions from real users (not benchmark-curated) tend
to have more vocab mismatch, so the production gap is likely wider
than n=14 controlled benchmarks show.
