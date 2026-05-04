# local-mgrep 0.9.0 — release notes

A measurement release. The 0.8.0 e2e benchmark with 6 easy single-shot
questions hinted that mgrep cuts an AI agent's tool-call count by ~54 %.
This release expands that benchmark to **14 hand-labelled questions
across Rust + Python + TypeScript including 8 hard semantic / vocab-
mismatch queries**, and reports the aggregate honestly — wins, losses,
and the per-task data so anyone can reproduce or audit.

## Headline

**Replacing rg-only with mgrep in Claude Code agent loops cuts tool
calls 30 % and improves answer correctness by 2 / 14 tasks** across Rust,
Python, and TypeScript hand-labelled questions. On the single hardest
semantic query in the set, mgrep cut tool calls 25× (25 → 1) and ran
1 / 8 the wall time.

|  | rg-only | mgrep-on | Δ |
|---|:-:|:-:|:-:|
| **Total tool calls (sum, 14 tasks)** | 124 | 87 | **−30 %** |
| **Strict-label correct** | 7 / 14 | 9 / 14 | **+2 tasks** |
| **Lenient-label correct (alts allowed)** | 9 / 14 | 11 / 14 | **+2 tasks** |
| Total tokens | 471 K | 528 K | +12 % (mgrep slightly more) |

## Why "−30 % tool calls" matters even when token cost is roughly equal

Each tool call is an LLM round trip + network RTT + serialization +
context-bloat in the agent's reasoning loop. **Fewer tool calls is
pure efficiency win on dimensions other than the token bill:**

  - Latency: each tool call adds 0.5-2 s of overhead in a real agent
    loop. -30 % tool calls means faster end-to-end.
  - Context bloat: tool inputs and outputs all live in the agent's
    context window. Fewer tool rounds = cleaner context = better
    reasoning quality on subsequent turns.
  - Agent quality: with mgrep returning the canonical file directly,
    the agent doesn't get distracted re-reading false-positive
    candidates. Strict label correctness improves +2 tasks.

The token-cost claim that the original cloud mgrep markets is real
for *static retrieval output* (we've measured 17.7× there in
`benchmarks/agent_context_benchmark.py`), but in a real agent reasoning
loop the agent's own thinking dominates the bill. Don't expect mgrep
to halve your Claude Code subscription cost; expect it to make the
agent **faster, simpler, and slightly more accurate**.

## Best case in the set (repo-A editor cursor)

The clearest single-task win is task 2 in the hard set ("How does the
in-line text editor handle cursor movement and keystroke input?"):

|  | rg-only | mgrep-on | Δ |
|---|:-:|:-:|:-:|
| Tool calls | 25 | 1 | **25× fewer** |
| Wall time | 128 s | 15 s | **1 / 8** |
| Tokens | 43 K | 28 K | −34 % |

mgrep returned `app/src/editor/view/mod.rs` on the first call. The
rg-only agent burned through 25 `rg` / `find` / `Read` calls
triangulating across the editor crate and the application's editor
module before settling on the same file. Pure efficiency delta.

## Worst case in the set (repo-A signin)

We also publish where mgrep didn't help:

|  | rg-only | mgrep-on | Δ |
|---|:-:|:-:|:-:|
| Tool calls | 5 | 7 | +40 % (mgrep slightly more) |
| Tokens | 31 K | 44 K | +44 % |

The signin question has clear vocabulary overlap (`auth`, `session`,
`token` all appear directly in path tokens), so rg's straightforward
scan was already efficient. The mgrep agent ran 4 mgrep calls to
explore neighbouring concepts (auth_manager, credentials, login_slide)
before answering — it wandered. Both got the right answer
(`app/src/auth/...`).

The pattern: mgrep helps most when query vocabulary doesn't match
code identifiers (semantic gap), and is roughly equal to rg when the
match is lexical-friendly.

## How the experiment was run

  - 14 questions: 6 easy from 0.8.0 + 8 hard new ones (4 repo-A Rust,
    2 repo-B Python, 2 repo-c TypeScript).
  - 28 sub-agents spawned via the Claude Code `Agent` tool in 2
    rounds (16 hard parallel + 12 easy from 0.8.0).
  - Two prompt conditions per question:
      - **rg-only**: explicitly forbade `mgrep`; allowed `rg`,
        `find`, `ls`, `head`, `cat`, `wc`, `Read`, `Grep`.
      - **mgrep-on**: instructed `mgrep` as primary tool; `Read`
        allowed for verification; `rg` / `grep` / `find` forbidden.
  - Each agent returned a single canonical-file JSON answer plus a
    `TOOLS:` audit line of every shell call. Tool-list audits
    confirm both conditions stayed within their allowed sets.
  - Token / tool-call / wall-time totals from each sub-agent's own
    `usage` telemetry.

## Caveats — what this benchmark can NOT claim

  - n = 14 tasks is a measurement, not a study. Statistical
    significance is weak.
  - Wall-time data is contaminated by 8-way parallel Ollama
    contention and is not in the headline. In single-user usage
    Ollama isn't contended.
  - Tool-list compliance was prompt-instructed, not API-enforced.
    Audit lines confirm compliance but don't prove it.
  - Tasks were drawn from existing per-repo `*.json` benchmarks.
    Genuinely novel questions (the kind a real user asks) are
    likely harder; the gap could widen on those.

## What did not change

  - Retrieval pipeline byte-for-byte 0.8.0.
  - All 0.4.x / 0.5.x / 0.6.x / 0.7.0 / 0.8.0 flags remain valid.
  - 40 / 40 unit tests pass.
  - Defaults: `OLLAMA_EMBED_MODEL=nomic-embed-text`,
    `OLLAMA_LLM_MODEL=qwen2.5:3b`,
    `OLLAMA_HYDE_MODEL=qwen2.5:3b`, `OLLAMA_KEEP_ALIVE=-1`.

## Files changed

  - `benchmarks/agent_e2e_results.md` — extended with 8 hard tasks
    and the 14-task aggregate.
  - `docs/parity-benchmarks.md` — "End-to-end agent benchmark
    (v0.8.0)" section now covers v0.9.0 hard tasks too; raw 14-row
    table; per-task analysis of best / worst cases.
  - `docs/local-mgrep-0.9.0.md` — these notes.
  - `docs/assets/hero-dark.svg` — version v0.8.0 → v0.9.0.
  - `docs/assets/og-image.svg` — recall tile updated to "−30 %
    AGENT TOOL CALLS · 14 hand-labelled tasks".
  - `pyproject.toml` — version bump.

## Compatibility

  - 40 / 40 unit tests pass.
  - All 0.8.0 flags / env / per-project DB layout unchanged.
  - Existing project indexes are picked up as-is.

## Install

```
pip install --upgrade local-mgrep
```
