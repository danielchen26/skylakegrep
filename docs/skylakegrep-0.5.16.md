# skylakegrep 0.5.16 — bounded agent latency and stronger scoped evidence

0.5.16 is a reliability and agent-performance release. It keeps the 0.5.15
agent presets, but makes their runtime contract stricter: machine-readable
agent calls now use bounded local-model timeouts, skip heavyweight foreground
refresh lanes, return a wider compact evidence set by default, and filter
low-evidence JSON candidates before they reach the next LLM step.

The goal is simple: when an agent calls `skygrep`, it should get useful
structured context quickly, and if the semantic index is still warming up,
the command should return bounded evidence or an honest empty result instead
of making the user wait indefinitely.

## What changed

- Agent and JSON calls now use explicit bounded budgets:
  `SKYGREP_AGENT_ROUTER_TIMEOUT_S` defaults to `1.5`,
  `SKYGREP_AGENT_MODEL_TIMEOUT_S` defaults to `3.0`, and
  `SKYGREP_AGENT_CASCADE_TIMEOUT_S` defaults to `8.0`.
- Human interactive searches still allow deeper foreground work by default:
  `SKYGREP_FOREGROUND_MODEL_TIMEOUT_S` defaults to `12.0`, and
  `SKYGREP_CASCADE_TIMEOUT_S` defaults to `30.0`.
- Local Ollama generation and router calls now pass request timeouts all the
  way down, so a slow model call degrades to fallback behavior instead of
  blocking the agent loop.
- Machine-readable agent paths no longer run expensive foreground refresh,
  symbol, or graph-prior maintenance before returning JSON context. Background
  indexing still continues.
- `--agent-context` now returns top-8 compact evidence by default, matching
  the documented long-form contract:
  `--json --content --detail standard --top 8 --no-rerank`.
- JSON agent results are filtered through a configurable evidence floor
  (`SKYGREP_AGENT_MIN_EVIDENCE_SCORE`, default `0.50`) to reduce misleading
  low-confidence context.
- Cold lazy indexing walks large directories incrementally under the foreground
  budget instead of blocking on full-tree discovery.
- Fast intent routing keeps pathlike phrases intact and adds more generic
  implementation-location prototypes, improving code-location queries without
  adding topic-specific trigger lists.

## Agent command contract

Use the same presets introduced in 0.5.15. The difference in 0.5.16 is that
the presets are now bounded in the runtime, not only documented:

```bash
skygrep --agent-fast "where is token refresh implemented?"
skygrep --agent-context --include "src/**" "what does token refresh do?"
skygrep serve --port 7878
skygrep --agent-daemon --agent-context "what does token refresh do?"
```

Escalate only when the next step needs it:

```bash
skygrep --content --detail full --include "docs/migration-plan.md" "show the deployment steps"
skygrep --answer --content "summarize the retry policy"
```

## Benchmark receipts

Release validation used only generic repository-maintenance queries and
placeholder examples. The private local prompts and paths used during
interactive debugging were not written into public release artifacts.

Targeted 0.5.16 smoke matrix on the current repository:

| Case | Shape | Result |
|---|---:|---:|
| T1 | scoped path-only | 1.945 s, 100 % expected-path coverage |
| T2 | scoped evidence | 5.117 s, 100 % coverage |
| T3 | router-timeout evidence | 4.840 s, 100 % coverage |
| T4 | cascade-timeout evidence | 4.805 s, 100 % coverage |
| T5 | setup-instruction discovery | 3.059 s, 100 % coverage |
| T6 | deep known-file read | 4.406 s, 100 % coverage |
| T7 | broad implementation lookup | 4.920 s, 100 % coverage |
| T8 | absent-concept negative control | 4.554 s, correctly returned no evidence |

The top-8 `--agent-context` retest for a multi-file implementation query
returned the expected auxiliary file that top-5 could miss before this release.

## Compatibility

- No index rebuild is required.
- Existing `skygrep setup` managed blocks should be refreshed so agents learn
  the current top-8 `--agent-context` contract and daemon-first guidance.
  `skygrep setup --check` audits that without modifying files.
- Users who intentionally want longer agent waits can raise the
  `SKYGREP_AGENT_*_TIMEOUT_S` variables, but the default is optimized for
  agent tool loops where stale or ambiguous evidence should fall back quickly.

## Verification

- Full test suite: `358 passed, 2 warnings, 20 subtests passed`.
- `git diff --check`: clean.
- `skygrep setup --check`: all managed snippets current.
- Source privacy scan and distribution privacy scan are required before
  publishing.

## Privacy

All public examples in this release use fictional placeholders such as
`token refresh`, `migration-plan.md`, and `retry policy`. No real user
prompt, local absolute path, private filename, screenshot, document category,
name, or email is included in the release notes, README, docs site, wheel, or
sdist.
