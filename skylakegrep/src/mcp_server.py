"""Native MCP stdio server MVP for skygrep.

Tools ``search`` and ``agent_context`` delegate to the same retrieval
pipelines used by ``skygrep search`` / ``skygrep --agent-context`` and the
HTTP daemon (``storage.search`` and ``candidate_recall.run_agent_context_search``).

Why not the official ``mcp`` package
------------------------------------
The PyPI ``mcp`` SDK requires Python >=3.10 and pulls a heavy stack
(pydantic, starlette, uvicorn, opentelemetry, …). This project targets
Python 3.9+ with a lean CLI dependency set, so the MVP speaks MCP over
JSON-RPC stdio with Content-Length framing (LSP-style) using only the
stdlib. Swap to the official SDK later if the Python floor rises.

CLI ``--agent-context`` vs MCP ``agent_context``
-----------------------------------------------
``skygrep --agent-context`` is a CLI preset that forces agent_mode=context
(JSON snippets, --content, detail=standard, top=8, --no-rerank) and then
runs ``run_agent_context_search``. The MCP tool ``agent_context`` exposes
that same retrieval contract as structured arguments (defaults aligned:
top_k=8, snippets on, no rerank). Field names use snake_case tool args
rather than CLI flags.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Optional, TextIO

from . import __version__
from . import auto_index as ai
from . import config as cfg_mod
from .candidate_recall import run_agent_context_search
from .embeddings import get_embedder
from .storage import init_db, search as storage_search

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "skygrep"

# Optional keys mirrored from the daemon / CLI JSON contract.
_OPTIONAL_RESULT_KEYS = (
    "fallback",
    "candidate_recall",
    "candidate_recall_lanes",
    "source_type",
    "search_intent",
    "evidence_terms",
    "why_ranked",
    "evidence_bundle",
    "supporting_chunks",
    "confidence",
    "agent_summary",
    "strict_verification",
    "filename_token",
    "source",
    "query_excerpts",
    "content_excerpt",
    "content_preview",
)


class McpToolError(Exception):
    """Structured tool failure surfaced to MCP clients."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


# Injected by tests; production uses get_embedder.
_embedder_factory: Optional[Callable[[], Any]] = None


def set_embedder_factory(factory: Optional[Callable[[], Any]]) -> None:
    """Override embedder construction (tests inject a mock; no live Ollama)."""

    global _embedder_factory
    _embedder_factory = factory


def _make_embedder():
    if _embedder_factory is not None:
        return _embedder_factory()
    return get_embedder(role="query")


def _probe_embedder(embedder: Any) -> None:
    """Raise ``embedder_down`` when an HTTP embed backend is unreachable.

    In-process / mock embedders (no ``base_url``) are assumed ready. The
    production ``OllamaEmbedder`` swallows request errors into zero vectors;
    probing ``/api/tags`` gives MCP callers a clear structured failure
    instead of silent garbage rankings.
    """

    base = getattr(embedder, "base_url", None)
    if not base:
        return
    try:
        import requests

        resp = requests.get(f"{str(base).rstrip('/')}/api/tags", timeout=2.0)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — map any transport failure
        raise McpToolError(
            "embedder_down",
            f"Embedder unreachable at {base}: {exc}",
        ) from exc


def _embed_query(embedder: Any, query: str) -> list[float]:
    try:
        _probe_embedder(embedder)
        vec = embedder.embed(query)
    except McpToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise McpToolError(
            "embedder_down",
            f"Embedder failed: {exc}",
        ) from exc
    if not isinstance(vec, list) or not vec:
        raise McpToolError("embedder_down", "Embedder returned an empty vector")
    return vec


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value if item is not None and str(item) != "")
    return (str(value),)


def resolve_workspace(
    path: str | None = None,
    *,
    default_path: str | None = None,
) -> tuple[Path, Path]:
    """Return ``(project_root, db_path)`` for a tool call.

    ``path`` (tool arg) wins over ``default_path`` (``skygrep mcp --path`` /
    ``SKYGREP_MCP_PATH``). Raises ``bad_path`` when the directory is missing.
    """

    raw = path or default_path or os.environ.get("SKYGREP_MCP_PATH") or "."
    root = Path(raw).expanduser()
    try:
        root = root.resolve()
    except OSError as exc:
        raise McpToolError("bad_path", f"Cannot resolve path {raw!r}: {exc}") from exc
    if not root.exists():
        raise McpToolError("bad_path", f"Path does not exist: {root}")
    if not root.is_dir():
        raise McpToolError("bad_path", f"Path is not a directory: {root}")

    project = cfg_mod.project_root(root)
    env_db = os.environ.get("SKYGREP_DB_PATH")
    if env_db:
        db_path = Path(env_db).expanduser().resolve()
    else:
        db_path = cfg_mod.project_db_path(project)
    return project, db_path


def open_index(db_path: Path) -> sqlite3.Connection:
    """Open the project index or raise ``no_index``."""

    if not db_path.exists():
        raise McpToolError(
            "no_index",
            f"No index at {db_path}. Run `skygrep index .` in the project first.",
        )
    conn = init_db(db_path)
    status = ai.index_status(conn)
    if status["chunks"] == 0:
        conn.close()
        raise McpToolError(
            "no_index",
            f"Index at {db_path} has zero chunks. Run `skygrep index .` first.",
        )
    return conn


def serialize_results(
    results: list[dict],
    *,
    include_snippet: bool = True,
    snippet_chars: int = 2000,
) -> list[dict]:
    """Match the daemon/CLI JSON result shape."""

    out: list[dict] = []
    for result in results:
        item: dict[str, Any] = {
            "path": result.get("path"),
            "start_line": result.get("start_line"),
            "end_line": result.get("end_line"),
            "language": result.get("language"),
            "score": float(result.get("score") or 0.0),
        }
        if include_snippet:
            snippet = result.get("snippet") or ""
            item["snippet"] = snippet[:snippet_chars]
        for key in _OPTIONAL_RESULT_KEYS:
            if key in result:
                item[key] = result[key]
        out.append(item)
    return out


def run_search(
    query: str,
    *,
    path: str | None = None,
    default_path: str | None = None,
    top_k: int = 10,
    languages: tuple[str, ...] | list[str] = (),
    include: tuple[str, ...] | list[str] = (),
    exclude: tuple[str, ...] | list[str] = (),
    multi_resolution: bool = True,
    file_top: int = 30,
    rerank: bool = False,
) -> dict[str, Any]:
    """Delegate to ``storage.search`` (same core path as the HTTP daemon)."""

    if not query or not str(query).strip():
        raise McpToolError("invalid_args", "query is required")

    project, db_path = resolve_workspace(path, default_path=default_path)
    conn = open_index(db_path)
    started = time.perf_counter()
    try:
        embedder = _make_embedder()
        query_embedding = _embed_query(embedder, str(query))
        results = storage_search(
            conn,
            query_embedding,
            top_k=int(top_k),
            languages=_as_str_tuple(languages),
            include_patterns=_as_str_tuple(include),
            exclude_patterns=_as_str_tuple(exclude),
            query_text=str(query),
            rerank=bool(rerank),
            multi_resolution=bool(multi_resolution),
            file_top=int(file_top),
        )
    finally:
        conn.close()
    return {
        "results": serialize_results(results, include_snippet=True),
        "latency_seconds": round(time.perf_counter() - started, 4),
        "project_root": str(project),
        "db_path": str(db_path),
        "tool": "search",
    }


def run_agent_context(
    query: str,
    *,
    path: str | None = None,
    default_path: str | None = None,
    top_k: int = 8,
    languages: tuple[str, ...] | list[str] = (),
    include: tuple[str, ...] | list[str] = (),
    exclude: tuple[str, ...] | list[str] = (),
    strict: bool = False,
    semantic_only: bool = False,
    support_per_path: int = 2,
    rg_timeout: float = 0.75,
) -> dict[str, Any]:
    """Delegate to ``run_agent_context_search`` (CLI ``--agent-context``)."""

    if not query or not str(query).strip():
        raise McpToolError("invalid_args", "query is required")

    project, db_path = resolve_workspace(path, default_path=default_path)
    conn = open_index(db_path)
    started = time.perf_counter()
    try:
        results, telemetry = run_agent_context_search(
            conn,
            str(query),
            project,
            top_k=int(top_k),
            languages=_as_str_tuple(languages),
            include_patterns=_as_str_tuple(include),
            exclude_patterns=_as_str_tuple(exclude),
            max_paths=max(int(top_k) * 8, int(top_k)),
            rg_timeout=float(rg_timeout),
            support_per_path=int(support_per_path),
            semantic_only=bool(semantic_only),
            strict=bool(strict),
            embedder_factory=_make_embedder,
            source_root=project,
        )
        for result in results:
            result.setdefault("fallback", "candidate-recall")
            result.setdefault("candidate_recall", True)
    finally:
        conn.close()
    return {
        "results": serialize_results(results, include_snippet=True),
        "latency_seconds": round(time.perf_counter() - started, 4),
        "project_root": str(project),
        "db_path": str(db_path),
        "telemetry": {
            "intent": telemetry.get("intent"),
            "total_paths": telemetry.get("total_paths"),
        },
        "tool": "agent_context",
    }


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "search",
        "description": (
            "Semantic / hybrid code search over the local skygrep index. "
            "Delegates to the same storage.search path as `skygrep search` "
            "and the skygrep HTTP daemon."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language or symbol query"},
                "path": {
                    "type": "string",
                    "description": "Project directory (defaults to cwd / SKYGREP_MCP_PATH)",
                },
                "top_k": {"type": "integer", "default": 10},
                "include": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Glob include patterns (CLI --include)",
                },
                "exclude": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Glob exclude patterns (CLI --exclude)",
                },
                "languages": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "rerank": {"type": "boolean", "default": False},
                "multi_resolution": {"type": "boolean", "default": True},
            },
            "required": ["query"],
        },
    },
    {
        "name": "agent_context",
        "description": (
            "Compact evidence pack for LLM agents (JSON snippets). "
            "Same retrieval contract as `skygrep --agent-context` / "
            "daemon agent_mode=context via run_agent_context_search."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "top_k": {
                    "type": "integer",
                    "default": 8,
                    "description": "Aligned with CLI --agent-context default top 8",
                },
                "include": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "languages": {"type": "array", "items": {"type": "string"}},
                "strict": {
                    "type": "boolean",
                    "default": False,
                    "description": "Same as CLI --strict (independent semantic agreement)",
                },
            },
            "required": ["query"],
        },
    },
]


def _tool_result_ok(payload: dict[str, Any]) -> dict[str, Any]:
    text = json.dumps(payload, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def _tool_result_err(exc: McpToolError) -> dict[str, Any]:
    payload = exc.as_dict()
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
        "structuredContent": payload,
        "isError": True,
    }


def call_tool(
    name: str,
    arguments: dict[str, Any] | None,
    *,
    default_path: str | None = None,
) -> dict[str, Any]:
    """Dispatch a tools/call request to the delegated pipelines."""

    args = dict(arguments or {})
    try:
        if name == "search":
            query = args.get("query")
            if not query:
                raise McpToolError("invalid_args", "search requires 'query'")
            payload = run_search(
                str(query),
                path=args.get("path"),
                default_path=default_path,
                top_k=int(args.get("top_k", 10)),
                languages=_as_str_tuple(args.get("languages")),
                include=_as_str_tuple(args.get("include")),
                exclude=_as_str_tuple(args.get("exclude")),
                multi_resolution=bool(args.get("multi_resolution", True)),
                file_top=int(args.get("file_top", 30)),
                rerank=bool(args.get("rerank", False)),
            )
            return _tool_result_ok(payload)
        if name == "agent_context":
            query = args.get("query")
            if not query:
                raise McpToolError("invalid_args", "agent_context requires 'query'")
            payload = run_agent_context(
                str(query),
                path=args.get("path"),
                default_path=default_path,
                top_k=int(args.get("top_k", 8)),
                languages=_as_str_tuple(args.get("languages")),
                include=_as_str_tuple(args.get("include")),
                exclude=_as_str_tuple(args.get("exclude")),
                strict=bool(args.get("strict", False)),
                semantic_only=bool(args.get("semantic_only", False)),
            )
            return _tool_result_ok(payload)
        raise McpToolError("unknown_tool", f"Unknown tool: {name}")
    except McpToolError as exc:
        return _tool_result_err(exc)


class McpServer:
    """Minimal MCP JSON-RPC handler (initialize / tools.*)."""

    def __init__(self, *, default_path: str | None = None):
        self.default_path = default_path
        self._initialized = False

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if "method" not in message:
            return self._error(message.get("id"), -32600, "Invalid Request")
        method = message["method"]
        msg_id = message.get("id")
        params = message.get("params") or {}

        # Notifications have no id and expect no response.
        is_notification = "id" not in message

        if method == "initialize":
            self._initialized = True
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": __version__,
                    },
                    "instructions": (
                        "skygrep MCP MVP: tools search and agent_context "
                        "delegate to local index pipelines. Index with "
                        "`skygrep index .` before calling tools."
                    ),
                },
            }
        if method == "notifications/initialized":
            return None
        if method == "ping":
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": TOOL_DEFINITIONS},
            }
        if method == "tools/call":
            name = params.get("name") or ""
            arguments = params.get("arguments") or {}
            result = call_tool(name, arguments, default_path=self.default_path)
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        if is_notification:
            return None
        return self._error(msg_id, -32601, f"Method not found: {method}")

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": code, "message": message},
        }


def _read_message(stdin: TextIO) -> dict[str, Any] | None:
    """Read one MCP message (Content-Length framing or a single JSON line)."""

    # Peek-style: read until we know the framing.
    # For Content-Length: headers then body. For NDJSON: one line of JSON.
    header_buf = ""
    while True:
        ch = stdin.read(1)
        if ch == "":
            if not header_buf.strip():
                return None
            # Incomplete stream
            line = header_buf.strip()
            if line:
                return json.loads(line)
            return None
        header_buf += ch
        if header_buf.endswith("\r\n\r\n") or header_buf.endswith("\n\n"):
            break
        # NDJSON: a full line that looks like JSON without headers.
        if ch == "\n" and "Content-Length" not in header_buf and ":" not in header_buf.split("\n")[0]:
            line = header_buf.strip()
            if not line:
                header_buf = ""
                continue
            return json.loads(line)

    headers = {}
    for raw_line in header_buf.splitlines():
        if ":" in raw_line:
            key, value = raw_line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    body = stdin.read(length)
    if not body:
        return None
    return json.loads(body)


def _write_message(stdout: TextIO, message: dict[str, Any]) -> None:
    payload = json.dumps(message, ensure_ascii=False)
    encoded = payload.encode("utf-8")
    stdout.write(f"Content-Length: {len(encoded)}\r\n\r\n")
    stdout.write(payload)
    stdout.flush()


def serve_stdio(
    *,
    default_path: str | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> None:
    """Run the MCP server on stdio until EOF."""

    server = McpServer(default_path=default_path)
    inn = stdin or sys.stdin
    out = stdout or sys.stdout
    # Ensure binary-safe line discipline for framing when possible.
    while True:
        try:
            message = _read_message(inn)
        except json.JSONDecodeError as exc:
            _write_message(
                out,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                },
            )
            continue
        if message is None:
            break
        try:
            response = server.handle(message)
        except Exception as exc:  # noqa: BLE001 — never crash the stdio loop
            logger.exception("MCP handler failed")
            if "id" in message:
                _write_message(
                    out,
                    {
                        "jsonrpc": "2.0",
                        "id": message.get("id"),
                        "error": {"code": -32603, "message": f"Internal error: {exc}"},
                    },
                )
            continue
        if response is not None:
            _write_message(out, response)


def main(argv: list[str] | None = None) -> None:
    """``python -m skylakegrep.src.mcp_server`` entry."""

    import argparse

    parser = argparse.ArgumentParser(prog="skygrep-mcp", description="skygrep MCP stdio server")
    parser.add_argument(
        "--path",
        default=None,
        help="Default project root for tool calls (also SKYGREP_MCP_PATH)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    serve_stdio(default_path=args.path)


if __name__ == "__main__":
    main()
