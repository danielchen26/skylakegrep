# SPDX-License-Identifier: Apache-2.0
"""Model Context Protocol server over stdio.

``skygrep setup`` writes markdown instructions into agent rules files
telling the agent to shell out to ``skygrep``. That only works for agents
that read those files, and it is invisible to every MCP client — Claude
Desktop, Cursor, Windsurf, Zed, VS Code — and to the MCP registries where
agents go looking for tools. This module closes that gap: agents call
skylakegrep as a structured tool with a declared schema instead of parsing
terminal output.

Design notes
------------
**Why subprocess the CLI instead of importing the pipeline.** Search
behaviour lives in a 4,200-line Click module whose routing, auto-index,
cascade, and strict-verification paths are what the test suite covers.
Re-entering that logic through a second door would create a second
behaviour to maintain and to get subtly wrong. The MCP tools therefore
invoke exactly the command a human would, with ``--json``, and inherit
every future CLI fix for free. Where latency matters, ``skygrep serve``
plus the agent daemon flags already amortise model load — that path is
unchanged and orthogonal to this one.

**Why hand-rolled JSON-RPC instead of the ``mcp`` SDK.** The official
Python SDK requires Python >= 3.10; this project supports 3.9, and the
whole point of the tool is to install anywhere without dragging a
dependency tree behind it. The stdio wire format is newline-delimited
JSON-RPC 2.0 and the server half of the handshake is three methods, so
the SDK buys nothing here.

**stdout is the transport.** Nothing but JSON-RPC may be written to it.
Diagnostics go to stderr.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Callable, Iterable, TextIO

from . import __version__

#: Protocol revision this server implements.
PROTOCOL_VERSION = "2025-06-18"
#: Revisions this server will accept from a client, newest first. A client
#: asking for anything else is answered with :data:`PROTOCOL_VERSION`, which
#: the spec's version-negotiation rule requires.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

SERVER_NAME = "skylakegrep"
SERVER_TITLE = "skylakegrep — offline semantic search"

INSTRUCTIONS = (
    "skylakegrep answers natural-language questions about files on this "
    "machine — code, markdown, PDFs, Word documents, plain text — and "
    "returns the path plus the exact line range. It runs fully offline "
    "against a local index; no file contents leave the machine.\n\n"
    "Prefer `search` over shelling out to grep or ripgrep when the query "
    "is about meaning rather than an exact string, and pass `path` as the "
    "project directory you are working in — the index is resolved from it. "
    "Use mode='locate' when you only need to know where something is "
    "(cheapest), and mode='context' when you need the surrounding lines to "
    "reason about. The first search in a project triggers indexing "
    "automatically; `index` is only needed to force a rebuild."
)

#: Wall-clock ceiling for one tool call. Cold-start indexing of a large
#: project is the slow case; MCP clients apply their own timeouts on top.
DEFAULT_TIMEOUT_SECONDS = 600

_RESULT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File containing the match."},
        # Null on purpose. The filename-shortcut route answers queries like
        # "find mcp_server.py" by matching the path itself, and a whole-file
        # match has no line range. Declaring these as required integers made
        # a strict client reject a correct result.
        "start_line": {
            "type": ["integer", "null"],
            "description": "First line, 1-indexed. Null for whole-file matches.",
        },
        "end_line": {
            "type": ["integer", "null"],
            "description": "Last line, inclusive. Null for whole-file matches.",
        },
        "language": {"type": ["string", "null"]},
        "score": {"type": "number", "description": "Retrieval score; higher is better."},
        "snippet": {
            "type": ["string", "null"],
            "description": "Matched lines. Absent in locate mode.",
        },
    },
    "required": ["path", "score"],
}

#: Diagnostics the CLI wrote to stderr while still exiting 0 — a missing
#: embedding model being the important one. Present only when non-empty, so
#: an empty result set with a warning is never mistaken for "no match".
_WARNINGS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Non-fatal diagnostics from the search backend. If this is present "
        "and results are empty, retrieval degraded — do not conclude the "
        "content is absent. Run `skygrep doctor` to see why."
    ),
}

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "search",
        "title": "Semantic file search",
        "description": (
            "Search files by meaning and get back paths with exact line "
            "ranges. Ask in plain language ('where is the auth token "
            "refreshed?'); 100+ languages are accepted. Fully offline."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language question, or a literal string.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Directory to search. The per-project index is resolved "
                        "from it. Defaults to the server's working directory."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["locate", "context"],
                    "default": "context",
                    "description": (
                        "'locate' returns paths and line ranges only and is the "
                        "cheapest. 'context' also returns the matched lines."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Maximum results. Defaults to 8.",
                },
                "include": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Glob patterns to restrict the search to.",
                },
            },
            "required": ["query"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "results": {"type": "array", "items": _RESULT_ITEM_SCHEMA},
                "count": {"type": "integer"},
                "warnings": _WARNINGS_SCHEMA,
            },
            "required": ["results", "count"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "index",
        "title": "Build or refresh the index",
        "description": (
            "Force an index build for a directory. Searching auto-indexes on "
            "first use, so this is only for forced rebuilds — after changing "
            "the embedding model, or to reindex from scratch with reset=true."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory to index. Defaults to the working directory.",
                },
                "reset": {
                    "type": "boolean",
                    "default": False,
                    "description": "Delete the existing index first, then rebuild.",
                },
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "output": {"type": "string"},
                "warnings": _WARNINGS_SCHEMA,
            },
            "required": ["output"],
        },
        # reset=true deletes an index. Destructive by MCP's definition, so
        # clients can gate it behind confirmation.
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False},
    },
    {
        "name": "stats",
        "title": "Index status",
        "description": (
            "Report what is indexed for a directory: file and chunk counts, "
            "embedding model, and index freshness."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Project directory. Defaults to the working directory.",
                }
            },
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "output": {"type": "string"},
                "warnings": _WARNINGS_SCHEMA,
            },
            "required": ["output"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
)

_TOOLS_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def server_command() -> str:
    """Absolute path to the ``skygrep`` entry point, for client configs.

    MCP clients are usually GUI applications that spawn servers without a
    login shell, so they do not inherit the PATH that made a bare
    ``skygrep`` work in a terminal — the most common way an MCP server
    silently fails to start. Emit an absolute path when one is
    discoverable, and fall back to the bare name only when it is not.
    """

    invoked = sys.argv[0] if sys.argv else ""
    if invoked and os.path.basename(invoked).startswith("skygrep"):
        resolved = os.path.abspath(invoked)
        if os.path.exists(resolved):
            return resolved
    from shutil import which

    return which("skygrep") or "skygrep"


def client_config() -> dict[str, Any]:
    """The ``mcpServers`` block to paste into an MCP client's config."""

    return {
        "mcpServers": {
            SERVER_NAME: {"command": server_command(), "args": ["mcp"]}
        }
    }


# JSON-RPC 2.0 error codes used here.
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602


class ToolError(Exception):
    """A tool ran and failed. Reported with ``isError: true``, not as a
    protocol error — the distinction the spec draws between "this request
    was malformed" and "the work did not succeed"."""


def _cli_argv(args: Iterable[str]) -> list[str]:
    return [sys.executable, "-m", "skylakegrep.src.cli", *args]


def _run_cli(
    args: Iterable[str],
    *,
    cwd: str | None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    """Run the CLI and return ``(stdout, stderr)``, or raise
    :class:`ToolError`.

    stderr is returned rather than discarded on success because the CLI
    degrades instead of failing: when the embedding model is missing,
    ``skygrep search`` writes ``embed failed … substituting zero vector``
    to stderr, prints an empty result list, and exits 0. An agent that
    only sees the empty list concludes the file does not exist, which is
    the wrong conclusion and an expensive one. Callers surface these
    lines as ``warnings`` so the model can tell "no match" apart from
    "retrieval is broken".
    """

    resolved = os.path.abspath(os.path.expanduser(cwd)) if cwd else None
    if resolved is not None and not os.path.isdir(resolved):
        raise ToolError(f"path is not a directory: {resolved}")
    try:
        proc = subprocess.run(
            _cli_argv(args),
            cwd=resolved,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"skygrep did not finish within {timeout}s") from None
    except OSError as exc:
        raise ToolError(f"could not start skygrep: {exc}") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ToolError(
            f"skygrep exited {proc.returncode}"
            + (f": {detail}" if detail else "")
        )
    return proc.stdout, proc.stderr


def _warnings(stderr: str) -> list[str]:
    return [line.strip() for line in (stderr or "").splitlines() if line.strip()]


def _tool_search(arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("`query` is required and must be a non-empty string")

    mode = arguments.get("mode", "context")
    if mode not in ("locate", "context"):
        raise ToolError("`mode` must be 'locate' or 'context'")

    args = ["search", query, "--json"]
    # The agent presets already pin --no-rerank and rule-based routing for
    # bounded latency, which is what a tool call wants.
    args.append("--agent-fast" if mode == "locate" else "--agent-context")

    limit = arguments.get("limit")
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ToolError("`limit` must be a positive integer")
        args += ["--top", str(limit)]

    include = arguments.get("include") or []
    if not isinstance(include, list) or any(not isinstance(p, str) for p in include):
        raise ToolError("`include` must be an array of glob strings")
    for pattern in include:
        args += ["--include", pattern]

    stdout, stderr = _run_cli(args, cwd=arguments.get("path"))
    try:
        results = json.loads(stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ToolError(f"could not parse skygrep JSON output: {exc}") from None
    if not isinstance(results, list):
        raise ToolError("skygrep returned JSON that was not a list of results")
    payload: dict[str, Any] = {"results": results, "count": len(results)}
    warnings = _warnings(stderr)
    if warnings:
        payload["warnings"] = warnings
    return payload


def _tool_index(arguments: dict[str, Any]) -> dict[str, Any]:
    args = ["index", "."]
    if arguments.get("reset"):
        args.append("--reset")
    stdout, stderr = _run_cli(args, cwd=arguments.get("path"))
    payload: dict[str, Any] = {"output": stdout.strip()}
    warnings = _warnings(stderr)
    if warnings:
        payload["warnings"] = warnings
    return payload


def _tool_stats(arguments: dict[str, Any]) -> dict[str, Any]:
    stdout, stderr = _run_cli(["stats"], cwd=arguments.get("path"))
    payload: dict[str, Any] = {"output": stdout.strip()}
    warnings = _warnings(stderr)
    if warnings:
        payload["warnings"] = warnings
    return payload


_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "search": _tool_search,
    "index": _tool_index,
    "stats": _tool_stats,
}


def _negotiate_protocol(requested: Any) -> str:
    if isinstance(requested, str) and requested in SUPPORTED_PROTOCOL_VERSIONS:
        return requested
    return PROTOCOL_VERSION


def handle_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Handle one JSON-RPC message. Returns the response, or ``None`` for
    notifications, which must not be answered."""

    if message.get("jsonrpc") != "2.0":
        return _error(message.get("id"), _INVALID_REQUEST, "expected jsonrpc 2.0")

    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return _error(msg_id, _INVALID_PARAMS, "`params` must be an object")

    # Notifications carry no id and get no reply.
    if msg_id is None:
        return None

    if method == "initialize":
        return _ok(
            msg_id,
            {
                "protocolVersion": _negotiate_protocol(params.get("protocolVersion")),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "title": SERVER_TITLE,
                    "version": __version__,
                },
                "instructions": INSTRUCTIONS,
            },
        )

    if method == "ping":
        return _ok(msg_id, {})

    if method == "tools/list":
        return _ok(msg_id, {"tools": [dict(tool) for tool in TOOLS]})

    if method == "tools/call":
        name = params.get("name")
        if name not in _TOOLS_BY_NAME:
            return _error(msg_id, _INVALID_PARAMS, f"Unknown tool: {name}")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _error(msg_id, _INVALID_PARAMS, "`arguments` must be an object")
        try:
            structured = _HANDLERS[name](arguments)
        except ToolError as exc:
            return _ok(
                msg_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )
        return _ok(
            msg_id,
            {
                # Structured content is the contract; the text block is the
                # backwards-compatible mirror the spec asks for.
                "content": [
                    {"type": "text", "text": json.dumps(structured, indent=2)}
                ],
                "structuredContent": structured,
                "isError": False,
            },
        )

    return _error(msg_id, _METHOD_NOT_FOUND, f"Unknown method: {method}")


def _ok(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve_stdio(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Run the stdio transport until the client closes the input stream."""

    source = stdin if stdin is not None else sys.stdin
    sink = stdout if stdout is not None else sys.stdout

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            _write(sink, _error(None, _PARSE_ERROR, f"invalid JSON: {exc}"))
            continue
        if not isinstance(message, dict):
            _write(sink, _error(None, _INVALID_REQUEST, "expected a JSON object"))
            continue
        response = handle_message(message)
        if response is not None:
            _write(sink, response)
    return 0


def _write(sink: TextIO, payload: dict[str, Any]) -> None:
    # Newline-delimited, no embedded newlines: the stdio framing rule.
    sink.write(json.dumps(payload) + "\n")
    sink.flush()
