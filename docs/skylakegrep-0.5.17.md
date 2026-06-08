# skylakegrep 0.5.17 — hybrid agent recall and refreshed LLM guidance

0.5.17 is an agent-quality release. It closes the earlier gap where
`skygrep --agent-context` saved a large amount of context but could still miss
some implementation-location or test-location anchors that brute-force `rg`
would eventually surface.

The key change is architectural: bounded `rg` is now an internal recall lane,
not a manual fallback that the caller has to remember. Agent context fuses path
tokens, symbols, SQLite chunk evidence, bounded ripgrep recall, source-type
priors, symbol anchors, and confidence summaries before the next LLM sees
anything.

## What changed

- `--agent-context` now builds a hybrid evidence bundle from independent lanes:
  path token recall, symbol recall, SQLite chunk recall, bounded ripgrep, and
  source-type priors.
- Agent JSON remains backward-compatible as a list of result objects, but
  results can now include `agent_summary`, `why_ranked`, `evidence_bundle`,
  `candidate_recall_lanes`, `source_type`, `search_intent`, and
  `evidence_terms`.
- Confidence is explicit. Top results carry `quality` as `best`, `degraded`,
  or `uncertain`, plus a scoped follow-up probe when the evidence is not sharp.
- Low-confidence semantic escalation is only used for `uncertain` cases, so
  normal agent calls avoid reintroducing local-model latency.
- File-first ranking now better handles common agent intents:
  test-location, config-location, indexing-rules, search-result logic,
  implementation location, and generic semantic evidence.
- Symbol anchors now bridge function/class names back to body chunks, fixing
  cases where tree-sitter separated a definition name from its implementation
  body.
- Managed LLM setup guidance is bumped to `agent-guidance-v4`. Claude, Codex,
  OpenCode, Gemini CLI, and Cursor instructions now teach agents to trust the
  automatic hybrid path first, inspect confidence fields, and reserve raw `rg`
  for regex or raw-output needs.

## Agent command contract

The default first pass for LLM tool loops remains:

```bash
skygrep --agent-context --include "src/**" "where is token refresh implemented?"
```

Read the returned JSON before escalating:

- `agent_summary.quality == "best"`: trust the anchors and read selected files.
- `agent_summary.quality == "degraded"`: usually proceed, but use the suggested
  scoped follow-up for high-risk work or when top files disagree.
- `agent_summary.quality == "uncertain"`: run the suggested follow-up probe or
  a narrower `--include` call.

Use raw `rg` directly only for exact regex, raw grep output, or when `skygrep`
is unavailable.

## Benchmark receipts

Fresh parity validation on 30 generic repository-maintenance tasks:

| Metric | real `rg` agent | `skygrep --agent-context` |
|---|---:|---:|
| Hit rate | 30/30 | 30/30 |
| Context tokens | 6,049,556 | 271,561 |
| Context reduction | - | 22.28x less |
| Estimated total tokens | 6,088,556 | 310,561 |
| Estimated total reduction | - | 19.61x less |
| Average hot-query latency | 0.519 s | 0.376 s |
| Tool calls | 179 | 30 |
| MRR | 0.0078 | 0.7333 |

Fresh index build for the same benchmark took 192.281 s. That is indexing and
embedding cost, not steady-state query latency.

## Compatibility

- No index rebuild is required for existing users.
- The JSON result top level is still a list, so existing consumers continue to
  parse it. New fields are optional.
- Run `skygrep setup` or `skygrep setup --check` after upgrading so managed
  LLM instructions refresh to `agent-guidance-v4`.

## Verification

- Full test suite: `366 passed, 2 warnings`.
- Fresh real-ripgrep parity benchmark: `skygrep_hit_rate = 30/30`,
  `rg_hit_rate = 30/30`, `skygrep_missing_ids = []`.
- Scoped source smoke: top result correctly resolves to the intended
  architecture-control implementation file with a bounded `--agent-context`
  call.

## Privacy

Public examples in this release use fictional placeholders such as
`token refresh` and `src/**`. No private local prompt, screenshot, personal
path, document title, name, or email is included in the release notes.
