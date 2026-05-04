# local-mgrep 0.8.0 — release notes

A measurement release. No retrieval-pipeline change, no recall regression. The
new content is the **end-to-end Claude Code agent benchmark** that closes
the last open evidence gap in `docs/parity-benchmarks.md` (the "what an
end-to-end agent claim would still need" section).

## Headline

**On real Claude Code agent runs over 6 hand-labelled questions in 3
languages: mgrep cuts the agent's tool-call count by 54 % and improves
answer correctness, at roughly equivalent total token cost.**

|  | rg-only | mgrep-on | Δ |
|---|:-:|:-:|:-:|
| Total tokens (6 tasks) | 194 403 | 201 926 | +3.9 % |
| Total tool calls | 46 | 21 | **−54 %** |
| Avg tool calls / task | 7.7 | 3.5 | **−54 %** |
| Strict-label correct | 4 / 6 | 5 / 6 | **+1 task** |

Per-task data, methodology, and caveats live in
[`benchmarks/agent_e2e_results.md`](../benchmarks/agent_e2e_results.md)
and the new "End-to-end agent benchmark (v0.8.0)" section of
[`docs/parity-benchmarks.md`](parity-benchmarks.md).

## How the experiment was run

  - 6 questions drawn from the v0.7.0 multi-language benchmark
    (2 per repo across repo-A Rust, repo-B Python,
    repo-C TypeScript).
  - 12 sub-agents spawned in parallel via the Claude Code `Agent` tool
    (general-purpose). Each agent received the same question with
    one of two prompts:
      - **rg-only**: explicitly forbade `mgrep`; allowed `rg`,
        `find`, `ls`, `head`, `cat`, `Read`, `Grep`.
      - **mgrep-on**: instructed `mgrep` as primary tool; `Read`
        allowed for verification; `rg` / `grep` / `find` forbidden.
  - Each agent reported a single canonical-file JSON answer plus a
    `TOOLS:` audit line of every shell call it made. Tool-list
    audits confirm both conditions stayed within their allowed sets.
  - Token / tool-call / wall-time totals from each sub-agent's own
    `usage` telemetry, returned by the Claude Code Agent tool.

## What the numbers actually show

The clean signal is **tool-call reduction**: 54 % across 6 tasks
isn't an artefact, it's a direct consequence of mgrep returning
semantic top-K instead of file lists that the agent then has to
narrow with several `Read` and `rg`-with-different-pattern calls.
Fewer tool calls = less context bloat in the agent's reasoning
loop = less cognitive overhead = better answers when the question
is hard.

The **token signal is roughly flat** (+3.9 %). The agent's own
reasoning tokens dominate; trimming the retrieval payload doesn't
move the needle on the total. Token-cost claims for mgrep should be
based on tool-call reduction multiplied by per-tool-call payload,
not on the agent's overall token usage.

The **wall-time signal is contaminated** by spawning 6 mgrep-on
agents in parallel against a single Ollama instance — the cascade-
escalation HyDE + embed calls queued behind each other, inflating
wall time on repo-B (117 s and 154 s vs ~30 s rg-only). In normal
single-user usage Ollama is not contended and warm queries land in
the 0.1-0.5 s range demonstrated in the 0.6.x demos. **The wall
time row in the benchmark table is flagged as not-clean** and is
not reported as a headline number.

## What did not change

  - Retrieval pipeline byte-for-byte 0.7.0.
  - All 0.4.x / 0.5.x / 0.6.x / 0.7.0 flags remain valid.
  - 40 / 40 unit tests pass.
  - Defaults: `OLLAMA_EMBED_MODEL=nomic-embed-text`,
    `OLLAMA_LLM_MODEL=qwen2.5:3b`,
    `OLLAMA_HYDE_MODEL=qwen2.5:3b`, `OLLAMA_KEEP_ALIVE=-1`.

## Files changed

  - `benchmarks/agent_e2e_results.md` — the raw per-task table,
    aggregate metrics, methodology, caveats.
  - `docs/parity-benchmarks.md` — new "End-to-end agent benchmark
    (v0.8.0)" section with the headline data and explicit
    relationship to the older simulated grep-agent benchmark.
  - `docs/local-mgrep-0.8.0.md` (new release notes).
  - `docs/assets/hero-dark.svg` — version v0.7.0 → v0.8.0.
  - `pyproject.toml` — version bump.

## Compatibility

  - 40 / 40 unit tests pass.
  - All 0.7.0 flags / env / per-project DB layout unchanged.
  - Existing project indexes are picked up as-is.

## Install

```
pip install --upgrade local-mgrep
```

## Honest framing — when to cite which number

  - **"mgrep saves on agent tool calls"**: cite the 0.8.0 e2e
    benchmark (54 % reduction, 6 tasks, real Claude Code agent
    loop).
  - **"mgrep saves on token volume"**: cite the simulated
    grep-agent benchmark from `agent_context_benchmark.py`
    (17.7 × static retrieval-output reduction, 30 tasks, no
    agent reasoning loop). This is what an agent *would* save
    if its reasoning didn't dominate; in practice the agent's
    own context-management dominates the bill.
  - **"mgrep helps recall on real questions"**: cite the v0.7.0
    multi-language benchmark (38 / 40, 95 % across 3 languages
    on hand-labelled questions, no agent harness).
