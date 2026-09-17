# Native MCP server (MVP)

`skygrep mcp` exposes a **stdio** Model Context Protocol server so agents
can call skygrep as structured tools instead of scraping shell output.

This is an **MVP**: two tools that **delegate** to the existing retrieval
pipelines. It is not marketed as a production-hardened MCP product yet.

**Client status:** protocol is generic stdio MCP. **Cursor** has been
verified end-to-end (Connected + 2 tools + live `agent_context` call).
Claude Desktop / Claude Code / other hosts use the same tools but need
their own config + a successful call — do not treat Cursor success as
universal out-of-the-box.

## Why a minimal JSON-RPC server (not the official `mcp` package)

The official PyPI [`mcp`](https://pypi.org/project/mcp/) SDK currently
requires **Python ≥ 3.10** and pulls a large dependency stack (pydantic,
starlette, uvicorn, OpenTelemetry, …). skylakegrep targets **Python 3.9+**
with a lean CLI footprint, so the MVP speaks MCP over JSON-RPC stdio with
Content-Length framing using only the standard library. The tool handlers
still call the same Python APIs as the CLI / HTTP daemon.

## Tools

| Tool | Delegates to | CLI analogue |
| --- | --- | --- |
| `search` | `storage.search` (+ query embedder) | `skygrep search` / daemon `POST /search` |
| `agent_context` | `candidate_recall.run_agent_context_search` | `skygrep --agent-context` / daemon `agent_mode=context` |

### CLI `--agent-context` vs MCP `agent_context`

`skygrep --agent-context` is a **CLI preset** that sets agent mode to
`context` (JSON snippets, content on, detail=standard, **top 8**, no
rerank) and then runs the shared agent-context retrieval contract.

The MCP tool `agent_context` exposes that **same retrieval contract** as
structured arguments. Defaults match the preset (`top_k=8`, snippets
included, no cross-encoder rerank). Prefer the MCP tool when the host
already speaks MCP; prefer the CLI flag for shell / scripts.

## Prerequisites

1. Install skylakegrep (`pip install skylakegrep` ≥0.7.5, or `pip install -e .` from a checkout).
2. Index the project: `skygrep index /path/to/repo`
3. Have a local embedder available for `search` (Ollama by default).
   Protocol tests mock the embedder and never call live Ollama.

A copy-paste starter lives at [`examples/mcp.json`](examples/mcp.json).

## Configure Cursor

Save as `.cursor/mcp.json` (project) or merge into your user MCP config:

```json
{
  "mcpServers": {
    "skygrep": {
      "command": "skygrep",
      "args": ["mcp", "--path", "/absolute/path/to/your/repo"],
      "env": {}
    }
  }
}
```

If `skygrep` is not on `PATH`, use the module form:

```json
{
  "mcpServers": {
    "skygrep": {
      "command": "python",
      "args": ["-m", "skylakegrep.src.mcp_server", "--path", "/absolute/path/to/your/repo"]
    }
  }
}
```

## Configure Claude Desktop

Add under `mcpServers` in Claude’s config file (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "skygrep": {
      "command": "skygrep",
      "args": ["mcp", "--path", "/absolute/path/to/your/repo"]
    }
  }
}
```

## Example tool calls

**search**

```json
{
  "name": "search",
  "arguments": {
    "query": "where is access token refresh implemented?",
    "path": "/absolute/path/to/your/repo",
    "top_k": 10,
    "include": ["src/**"]
  }
}
```

**agent_context**

```json
{
  "name": "agent_context",
  "arguments": {
    "query": "what does token refresh do?",
    "path": "/absolute/path/to/your/repo",
    "top_k": 8,
    "include": ["src/**"]
  }
}
```

## Structured errors

Failed tool calls return `isError: true` with JSON:

| `error` | Meaning |
| --- | --- |
| `no_index` | DB missing or zero chunks — run `skygrep index .` |
| `bad_path` | `path` does not exist or is not a directory |
| `embedder_down` | Query embedder unreachable / raised (e.g. Ollama down) |
| `invalid_args` | Missing required `query` (or similar) |

## Run manually

```bash
skygrep mcp --path /path/to/repo
# or
python -m skylakegrep.src.mcp_server --path /path/to/repo
```

The process speaks MCP on stdin/stdout; do not type interactively unless
you are framing JSON-RPC messages correctly.
