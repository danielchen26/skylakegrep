# 0.7.5 之后未穷尽项（跟进清单）

产品 **0.7.5 已发布**（PyPI + Cursor MCP 第 4 条已过）。下列项用于 **扩大 claim**，不是安装阻塞。

| # | 任务 | 服务哪类 claim | 状态 |
| --- | --- | --- | --- |
| 1 | 合入草稿 [#10](https://github.com/danielchen26/skylakegrep/pull/10)，按真实 `--agent-fast` / `--agent-context` 路径重跑六仓 General Benchmark，产出新 receipt | **latency / 测速路径** 类对外数字 | 未穷尽 |
| 2 | 真机大仓 smoke（不止 Cursor 小仓） | 「**大项目也好用**」 | 未穷尽 |
| 3 | Claude Desktop / Claude Code 等其它 MCP 客户端各自 Reload + 成功调用一次 | 「**不止 Cursor**」 | 未穷尽（协议已通用） |
| 4 | scale / noise 压力场景 | 更大规模、更吵环境下的 **稳性加码** | 可选 |
| 5 | 首页其余观感打磨（若仍发现旧版本叙事） | **对外观感一致** | 随本 docs PR 大部分已刷 |

## 已可对外说的（不要加码）

- Apache-2.0 · offline · CLI / `agent_context` harness · MCP MVP（`search` / `agent_context`；Cursor 已验）
- `pip install skylakegrep==0.7.5`
- 检索零件，不是全能 agent

## 明确不要说的（直到对应项关闭）

- 「延迟路径也全面验完 / 全面更快」（等 #10 + 新 receipt）
- 「所有 MCP 客户端开箱即用」
- 「comprehensive benchmark 全部做完」
