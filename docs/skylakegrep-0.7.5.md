# skylakegrep 0.7.5 — Native MCP MVP + stdio framing

0.7.5 ships the native MCP server to PyPI and fixes Content-Length framing
for Cursor / Claude Code pipes.

## What changed

- **Native MCP MVP (#22).** `skygrep mcp` / `skygrep-mcp` expose tools
  `search` and `agent_context` over JSON-RPC stdio (same retrieval
  contract as CLI `--agent-context` for the latter). See `docs/mcp.md`.
- **Binary stdio framing (#23).** Read/write via `stdin.buffer` /
  `stdout.buffer` so Content-Length matches raw pipe clients (fixes
  Cursor 0-tools / Claude Code `CONNECTION_CLOSED` class failures).

## Compatibility

- Still **Python 3.9+**; official `mcp` SDK is **not** a package dependency
  (optional host-side FastMCP wrapper on 3.10+ remains a client-specific
  workaround, not the default install).
- Apache-2.0, offline-first. Existing indexes untouched.

## Verification

- Protocol tests in CI (3.9–3.12).
- Cursor: Connected + 2 tools + live `agent_context` call with path/snippet.
- Other MCP clients: same protocol, configure and verify separately —
  not claimed as universal out-of-the-box.

## Positioning

Apache-2.0 · offline · CLI / `agent_context` harness · **MCP MVP
(`search` / `agent_context`) usable** — not a do-everything production
agent platform.
