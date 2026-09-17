# Follow-ups after 0.7.5

Product **0.7.5 is shipped** (PyPI + Cursor MCP gate passed). The items below expand **claims**; they are not install blockers.

| # | Task | Claim it supports | Status |
| --- | --- | --- | --- |
| 1 | Land draft [#10](https://github.com/danielchen26/skylakegrep/pull/10), re-run the six-repo General Benchmark on real `--agent-fast` / `--agent-context` paths, publish a new receipt | **Latency / measured speed-path** numbers | Open |
| 2 | Large-repo real-machine smoke (beyond the small Cursor smoke repo) | “Works on **big projects**” | Open |
| 3 | Verify other MCP clients once each (Claude Desktop / Claude Code, …) | “**Not only Cursor**” | Open (protocol is shared) |
| 4 | scale / noise stress | Optional **robustness** at larger/noisier scale | Optional |
| 5 | Leftover homepage polish if any stale narrative remains | Public **docs consistency** | Mostly done in docs #25 |

## Safe to say now (do not inflate)

- Apache-2.0 · offline · CLI / `agent_context` harness · MCP MVP (`search` / `agent_context`; Cursor verified)
- `pip install skylakegrep==0.7.5`
- A retrieval component for existing agents — not a do-everything agent

## Do not say until the matching row closes

- “Latency path fully verified / comprehensively faster” (needs #10 + new receipt)
- “Every MCP client works out of the box”
- “Comprehensive benchmark is finished”

Owner: @youseihuayu-wonderful (tracking [#26](https://github.com/danielchen26/skylakegrep/issues/26)).
