# SPDX-License-Identifier: Apache-2.0
"""MCP server protocol conformance.

These tests pin the wire contract, not the search quality: an MCP client
that cannot complete the handshake, or that receives a malformed
``tools/call`` result, sees the whole integration as broken regardless of
how good retrieval is. Search behaviour itself stays covered by the CLI
suite, which is exactly why the tools shell out to the CLI.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from skylakegrep import __version__
from skylakegrep.src import mcp_server

REPO_ROOT = Path(__file__).resolve().parents[1]


def call(method: str, params: dict | None = None, msg_id: int | None = 1):
    message = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        message["id"] = msg_id
    if params is not None:
        message["params"] = params
    return mcp_server.handle_message(message)


# --- handshake ---------------------------------------------------------


def test_initialize_echoes_a_supported_protocol_version():
    result = call("initialize", {"protocolVersion": "2025-03-26"})["result"]
    assert result["protocolVersion"] == "2025-03-26"


def test_initialize_answers_unknown_version_with_our_own():
    """Spec: if the client's version is unsupported, respond with ours."""

    result = call("initialize", {"protocolVersion": "1.0.0"})["result"]
    assert result["protocolVersion"] == mcp_server.PROTOCOL_VERSION


def test_initialize_declares_tools_capability_and_server_identity():
    result = call("initialize", {})["result"]
    assert "tools" in result["capabilities"]
    assert result["serverInfo"] == {
        "name": "skylakegrep",
        "title": mcp_server.SERVER_TITLE,
        "version": __version__,
    }
    assert "offline" in result["instructions"]


def test_notifications_get_no_response():
    """A JSON-RPC notification carries no id and must not be answered."""

    assert call("notifications/initialized", msg_id=None) is None


def test_ping_is_answered_with_an_empty_result():
    assert call("ping")["result"] == {}


def test_unknown_method_is_a_protocol_error():
    error = call("resources/list")["error"]
    assert error["code"] == -32601


def test_non_2_0_jsonrpc_is_rejected():
    response = mcp_server.handle_message({"jsonrpc": "1.0", "id": 1, "method": "ping"})
    assert response["error"]["code"] == -32600


# --- tool declarations ------------------------------------------------


def test_tools_list_exposes_search_index_stats():
    tools = call("tools/list")["result"]["tools"]
    assert [t["name"] for t in tools] == ["search", "index", "stats"]


@pytest.mark.parametrize("tool", mcp_server.TOOLS, ids=lambda t: t["name"])
def test_every_tool_declares_a_usable_schema(tool):
    assert tool["description"].strip()
    assert tool["inputSchema"]["type"] == "object"
    # An output schema obliges us to return conforming structuredContent.
    assert tool["outputSchema"]["type"] == "object"
    assert tool["outputSchema"]["required"]


def test_destructive_index_is_flagged_and_read_only_tools_are_not():
    by_name = {t["name"]: t for t in mcp_server.TOOLS}
    assert by_name["index"]["annotations"]["destructiveHint"] is True
    assert by_name["search"]["annotations"]["readOnlyHint"] is True
    assert by_name["stats"]["annotations"]["readOnlyHint"] is True


# --- tools/call -------------------------------------------------------


def test_unknown_tool_is_a_protocol_error_not_a_tool_error():
    response = call("tools/call", {"name": "rm_rf", "arguments": {}})
    assert response["error"]["code"] == -32602
    assert "rm_rf" in response["error"]["message"]


@pytest.mark.parametrize(
    "arguments,expected",
    [
        ({}, "`query` is required"),
        ({"query": "   "}, "`query` is required"),
        ({"query": "x", "mode": "telepathy"}, "`mode` must be"),
        ({"query": "x", "limit": 0}, "`limit` must be"),
        ({"query": "x", "limit": True}, "`limit` must be"),
        ({"query": "x", "include": "src/**"}, "`include` must be"),
    ],
)
def test_bad_search_arguments_become_tool_errors(arguments, expected):
    """Invalid input is a tool execution error, so the agent can read the
    message and retry instead of the client tearing down the session."""

    result = call("tools/call", {"name": "search", "arguments": arguments})["result"]
    assert result["isError"] is True
    assert expected in result["content"][0]["text"]


def test_search_reports_a_missing_directory_instead_of_searching_the_cwd():
    result = call(
        "tools/call",
        {"name": "search", "arguments": {"query": "x", "path": "/nonexistent-dir-xyz"}},
    )["result"]
    assert result["isError"] is True
    assert "not a directory" in result["content"][0]["text"]


def test_search_returns_structured_content_matching_its_output_schema(monkeypatch):
    payload = [
        {
            "path": "src/auth.py",
            "start_line": 10,
            "end_line": 20,
            "language": "python",
            "score": 0.87,
            "snippet": "def refresh_token():",
        }
    ]
    monkeypatch.setattr(
        mcp_server, "_run_cli", lambda *a, **k: (json.dumps(payload), "")
    )
    result = call("tools/call", {"name": "search", "arguments": {"query": "auth"}})[
        "result"
    ]
    assert result["isError"] is False
    assert result["structuredContent"] == {"results": payload, "count": 1}
    # Backwards compatibility: the same data mirrored as a text block.
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]


def test_search_mode_selects_the_matching_agent_preset(monkeypatch):
    seen: list[list[str]] = []

    def fake(args, **kwargs):
        seen.append(list(args))
        return "[]", ""

    monkeypatch.setattr(mcp_server, "_run_cli", fake)
    call("tools/call", {"name": "search", "arguments": {"query": "q", "mode": "locate"}})
    call("tools/call", {"name": "search", "arguments": {"query": "q"}})
    assert "--agent-fast" in seen[0] and "--agent-context" not in seen[0]
    assert "--agent-context" in seen[1] and "--agent-fast" not in seen[1]


def test_search_passes_limit_and_include_through(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        mcp_server, "_run_cli", lambda args, **k: (seen.append(list(args)), ("[]", ""))[1]
    )
    call(
        "tools/call",
        {
            "name": "search",
            "arguments": {"query": "q", "limit": 3, "include": ["src/**", "docs/**"]},
        },
    )
    args = seen[0]
    assert args[args.index("--top") + 1] == "3"
    assert args.count("--include") == 2


def test_unparseable_cli_output_is_a_tool_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(mcp_server, "_run_cli", lambda *a, **k: ("not json", ""))
    result = call("tools/call", {"name": "search", "arguments": {"query": "q"}})["result"]
    assert result["isError"] is True
    assert "could not parse" in result["content"][0]["text"]


def test_cli_failure_surfaces_stderr_to_the_agent(monkeypatch):
    def boom(*a, **k):
        raise mcp_server.ToolError("skygrep exited 2: ollama unreachable")

    monkeypatch.setattr(mcp_server, "_run_cli", boom)
    result = call("tools/call", {"name": "stats", "arguments": {}})["result"]
    assert result["isError"] is True
    assert "ollama unreachable" in result["content"][0]["text"]


def test_index_reset_reaches_the_cli(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        mcp_server, "_run_cli", lambda args, **k: (seen.append(list(args)), ("done", ""))[1]
    )
    call("tools/call", {"name": "index", "arguments": {"reset": True}})
    assert seen[0] == ["index", ".", "--reset"]


def test_degraded_retrieval_is_reported_as_warnings_not_as_no_match(monkeypatch):
    """The failure this exists for: `skygrep search` writes
    "embed failed … substituting zero vector" to stderr, prints `[]`, and
    exits 0 when the embedding model is missing. An agent must be able to
    tell that apart from "the content is not there"."""

    monkeypatch.setattr(
        mcp_server,
        "_run_cli",
        lambda *a, **k: (
            "[]",
            "embed failed for chunk of 24 chars: 404 Client Error\n",
        ),
    )
    result = call("tools/call", {"name": "search", "arguments": {"query": "q"}})["result"]
    assert result["isError"] is False
    structured = result["structuredContent"]
    assert structured["count"] == 0
    assert structured["warnings"] == [
        "embed failed for chunk of 24 chars: 404 Client Error"
    ]


def test_clean_runs_omit_the_warnings_key(monkeypatch):
    monkeypatch.setattr(mcp_server, "_run_cli", lambda *a, **k: ("[]", "  \n"))
    structured = call("tools/call", {"name": "search", "arguments": {"query": "q"}})[
        "result"
    ]["structuredContent"]
    assert "warnings" not in structured


@pytest.mark.parametrize("tool", ["index", "stats"])
def test_warnings_reach_every_tool(tool, monkeypatch):
    monkeypatch.setattr(mcp_server, "_run_cli", lambda *a, **k: ("ok", "ollama slow\n"))
    structured = call("tools/call", {"name": tool, "arguments": {}})["result"][
        "structuredContent"
    ]
    assert structured == {"output": "ok", "warnings": ["ollama slow"]}


@pytest.mark.parametrize("tool", mcp_server.TOOLS, ids=lambda t: t["name"])
def test_warnings_are_declared_in_every_output_schema(tool):
    """structuredContent must conform to the declared schema, so a key we
    can emit has to be in it."""

    assert "warnings" in tool["outputSchema"]["properties"]


def _conforms(item: dict, schema: dict) -> list[str]:
    """Type-check one object against a JSON Schema subset, driven by the
    schema the server actually declares rather than a copy of it."""

    py_types = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "null": type(None),
        "array": list,
        "object": dict,
    }
    problems = []
    for key in schema["required"]:
        if key not in item:
            problems.append(f"missing required key {key!r}")
    for key, value in item.items():
        declared = schema["properties"].get(key)
        if declared is None:
            continue
        names = declared["type"]
        allowed = tuple(
            py_types[n] for n in ([names] if isinstance(names, str) else names)
        )
        flat = tuple(
            t for a in allowed for t in (a if isinstance(a, tuple) else (a,))
        )
        if not isinstance(value, flat) or (
            isinstance(value, bool) and bool not in flat
        ):
            problems.append(f"{key}={value!r} violates type {names}")
    return problems


def test_whole_file_matches_conform_to_the_declared_result_schema():
    """The filename-shortcut route returns a path with no line range. That
    is a correct result, and the declared schema must accept it — an earlier
    version required integer line numbers and would have made strict
    clients reject real output."""

    whole_file_hit = {
        "path": "/repo/skylakegrep/src/mcp_server.py",
        "start_line": None,
        "end_line": None,
        "language": None,
        "score": 1.0,
    }
    assert _conforms(whole_file_hit, mcp_server._RESULT_ITEM_SCHEMA) == []


def test_line_ranged_matches_also_conform():
    chunk_hit = {
        "path": "src/auth.py",
        "start_line": 10,
        "end_line": 20,
        "language": "python",
        "score": 0.87,
        "snippet": "def refresh_token():",
    }
    assert _conforms(chunk_hit, mcp_server._RESULT_ITEM_SCHEMA) == []


def test_the_conformance_checker_actually_rejects_bad_shapes():
    """A validator that never fails would make the two tests above
    meaningless."""

    assert _conforms({"score": 1.0}, mcp_server._RESULT_ITEM_SCHEMA) == [
        "missing required key 'path'"
    ]
    problems = _conforms(
        {"path": "a", "score": 1.0, "start_line": "ten"},
        mcp_server._RESULT_ITEM_SCHEMA,
    )
    assert problems and "start_line" in problems[0]


# --- transport --------------------------------------------------------


def test_stdio_loop_answers_requests_and_skips_notifications():
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                "",
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()
    assert mcp_server.serve_stdio(stdin, stdout) == 0

    lines = [line for line in stdout.getvalue().splitlines() if line]
    assert len(lines) == 2, "the notification and the blank line must not be answered"
    assert [json.loads(line)["id"] for line in lines] == [1, 2]


def test_stdio_frames_are_single_line_json():
    """Newline-delimited framing breaks if a payload contains a newline."""

    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
    )
    stdout = io.StringIO()
    mcp_server.serve_stdio(stdin, stdout)
    assert stdout.getvalue().count("\n") == 1


def test_malformed_json_gets_a_parse_error_and_the_loop_survives():
    stdin = io.StringIO(
        "{not json\n"
        + json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"})
        + "\n"
    )
    stdout = io.StringIO()
    mcp_server.serve_stdio(stdin, stdout)
    first, second = [json.loads(line) for line in stdout.getvalue().splitlines() if line]
    assert first["error"]["code"] == -32700
    assert second["id"] == 7


def test_client_config_emits_an_absolute_command():
    config = mcp_server.client_config()["mcpServers"]["skylakegrep"]
    assert config["args"] == ["mcp"]
    # GUI MCP clients do not inherit a shell PATH, so a bare name is a trap.
    assert config["command"].startswith("/") or config["command"] == "skygrep"


# --- end-to-end over a real process -----------------------------------


def test_real_process_completes_a_handshake_and_lists_tools():
    """The transport must work through an actual spawned server, which is
    how every MCP client will use it."""

    requests = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": mcp_server.PROTOCOL_VERSION},
            }
        )
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        + "\n"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "skylakegrep.src.cli", "mcp"],
        input=requests,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    responses = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert [r["id"] for r in responses] == [1, 2]
    assert responses[0]["result"]["serverInfo"]["name"] == "skylakegrep"
    assert {t["name"] for t in responses[1]["result"]["tools"]} == {
        "search",
        "index",
        "stats",
    }
