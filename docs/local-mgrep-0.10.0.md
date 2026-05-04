# local-mgrep 0.10.0 — release notes

A measurement release. No retrieval-pipeline change, no recall regression.
Two new benchmark dimensions on top of the v0.9.0 e2e Claude Code agent
data: **multi-turn sessions** (the real-user pattern) and a **larger
single-turn sample** (6 unused tasks → 20 total).

## Headline

**−82 % Claude Code tool calls in multi-turn sessions, −37.6 % across
20 single-turn hand-labelled tasks** in Rust + Python + TypeScript.
On 5 of 6 medium-difficulty single-turn tasks, mgrep finds the
canonical file in **1** tool call vs rg-only's 4-8.

| Bench | Tasks × conds | rg-only tools | mgrep tools | Δ tools | Δ tokens |
|---|:-:|:-:|:-:|:-:|:-:|
| **0.10.0 multi-turn (repo-A 3-turn session)** | 1 × 3 | 38 | **7** | **−82 %** | −5 % |
| **0.10.0 single-turn (6 medium tasks)** | 6 | 25 | **6** | **−76 %** | −8 % |
| 20-task aggregate (0.8.0 + 0.9.0 + 0.10.0 single-turn) | 20 | 149 | **93** | **−37.6 %** | +6.5 % |

## What was tested

### B — multi-turn 3-turn repo-A session

Three sequential follow-up questions about the same area
(LLM-backend → streaming-into-UI → retry/error-handling). Each
turn's prompt carries the prior turn's Q + A as context, simulating
how a real Claude Code session accumulates state. The agent's own
tool-call history grows turn over turn.

|  | rg-only | mgrep-on |
|---|:-:|:-:|
| T1 tool calls | 23 | 1 |
| T2 tool calls | 7 | 5 |
| T3 tool calls | 8 | 1 |
| **Total tool calls** | **38** | **7** (5.4 × fewer) |
| Total tokens | 114 862 | 108 959 (−5 %) |
| Wall time | 179 s | 158 s (−12 %) |

The multi-turn pattern shows mgrep's tool-call advantage compounds:
by T3 the rg agent is still re-hunting through `rg` / `Read` while
the mgrep agent has the answer in 1 call.

### C — 6 unused medium-difficulty single-turn tasks

Drawn from the existing 40-task multi-language benchmark (the 6 not
already used in 0.8.0 or 0.9.0):

| Task | rg-only | mgrep | Δ tools |
|---|:-:|:-:|:-:|
| repo-A computer_use | 28 849 / 3 | 30 864 / **1** | −67 % |
| repo-A fuzzy_match | 32 038 / 4 | 29 208 / **1** | −75 % |
| repo-B <component-w> | 28 630 / 2 | 29 325 / **1** | −50 % |
| repo-B <component-v> | 32 981 / 8 | 27 544 / **1** | −88 % |
| repo-c keybindings parser | 32 320 / 4 | 27 805 / **1** | −75 % |
| repo-c LSPClient | 32 623 / 4 | 28 158 / **1** | −75 % |
| **Sum** | **187 441 / 25** | **172 904 / 6** | **−76 %** |

5 of 6 mgrep agents finished in **1 tool call**. Token total **−8 %**.
All 6 mgrep agents got correct answers (lenient label match);
strict label match 5/6.

## Combined 20-task single-turn aggregate

|  | rg-only | mgrep-on | Δ |
|---|:-:|:-:|:-:|
| Total tokens (sum) | 658 631 | 700 600 | +6.5 % (mgrep slightly more) |
| **Total tool calls (sum)** | **149** | **93** | **−37.6 %** |
| Avg tool calls / task | 7.5 | 4.7 | **−37.6 %** |
| Strict correct | 12 / 20 | 14 / 20 | +2 |
| Lenient correct | 14 / 20 | 17 / 20 | +3 |

## What the data actually shows

  - **Tool-call reduction is the cleanest, most consistent signal.**
    Across single-turn (−37.6 %) and multi-turn (−82 %) the
    direction is the same; multi-turn amplifies the gap.
  - **Tokens are noisy.** −5 % to −8 % on subsets where mgrep
    agents are decisive (1 tool call); +6 % on the noisier 0.9.0
    hard-task subset where mgrep agents wandered. Net: roughly
    flat. **Don't claim mgrep saves the LLM bill.**
  - **Quality slightly better with mgrep.** +2 strict, +3 lenient
    on the 20-task aggregate. mgrep solves the repo-A `<domain-y>_v6`
    famous miss; doesn't lose any task rg-only got right.
  - **Wall-time data is contaminated** by parallel Ollama
    contention from the benchmark methodology. Single-user usage
    has none of this. The 3-turn multi-turn session ran with a
    single mgrep agent at a time and saw mgrep wall time
    consistent with rg-only (179 s vs 158 s).

## Why this matters even when token cost is roughly flat

Each tool call in a Claude Code agent loop costs:
- An LLM round-trip (request + response).
- Network RTT (~0.5-2 s).
- Serialization / deserialization overhead.
- Context-window growth (tool inputs and outputs accumulate).

A 30-80 % tool-call reduction means the agent loop is **shorter,
faster, and cleaner** even when total tokens are equal. It's a
different efficiency dimension from the LLM bill.

## Caveats

  - n = 20 single-turn + 1 multi-turn session. Statistical
    significance is improving but still a measurement, not a study.
  - Wall-time data is noisy for the parallel-spawn rounds.
    Multi-turn was sequential and is reportable.
  - Tool-list compliance was prompt-instructed, not API-enforced.
    Audit lines confirm compliance.
  - Real-world questions (not benchmark-curated) likely have more
    vocabulary mismatch, so the production gap likely widens.

## What did not change

  - Retrieval pipeline byte-for-byte 0.9.0.
  - All 0.4.x – 0.9.0 flags remain valid.
  - 40 / 40 unit tests pass.

## Files changed

  - `benchmarks/agent_e2e_results.md` — extended with B (multi-turn)
    and C (6 medium tasks) sections, 20-task aggregate.
  - `docs/local-mgrep-0.10.0.md` (this file).
  - `docs/parity-benchmarks.md` — refreshed end-to-end agent
    benchmark section with 20-task aggregate and multi-turn row.
  - `docs/assets/og-image.{svg,png}` — recall tile updated to
    "−82 % MULTI-TURN TOOL CALLS · 3-turn repo-A session".
  - `docs/assets/hero-dark.svg` — version v0.9.0 → v0.10.0.
  - `pyproject.toml` — version bump.
  - `README.md` — Releases list adds 0.10.0; Performance section
    references 20-task aggregate.

## Compatibility

  - 40 / 40 unit tests pass.
  - All 0.9.0 flags / env / per-project DB layout unchanged.
  - Existing project indexes are picked up as-is.

## Install

```
pip install --upgrade local-mgrep
```
