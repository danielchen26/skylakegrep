"""CI-friendly MCP protocol tests (no live Ollama)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from skylakegrep.src import cli as cli_module
from skylakegrep.src import mcp_server
from skylakegrep.src.mcp_server import McpServer, McpToolError, call_tool, set_embedder_factory
from skylakegrep.src.storage import init_db, populate_file_embeddings, store_chunks_batch


class _StaticEmbedder:
    """Deterministic mock embedder — same pattern as daemon/search tests."""

    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0] if "token" in text.lower() else [0.0, 1.0]


class _RaisingEmbedder:
    def embed(self, text: str) -> list[float]:
        raise ConnectionError("ollama refused connection")


def _seed_index(root: Path) -> Path:
    target = root / "src" / "token_refresh.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def refresh_access_token():\n    return 'fresh token'\n",
        encoding="utf-8",
    )
    db_path = root / "index.db"
    conn = init_db(db_path)
    store_chunks_batch(
        conn,
        [
            {
                "file": str(target),
                "chunk": target.read_text(encoding="utf-8"),
                "language": "python",
                "chunk_index": 0,
                "file_mtime": target.stat().st_mtime,
                "start_line": 1,
                "end_line": 2,
                "start_byte": 0,
                "end_byte": target.stat().st_size,
                "embedding": [1.0, 0.0],
            }
        ],
    )
    populate_file_embeddings(conn)
    conn.close()
    return db_path


class McpProtocolTests(unittest.TestCase):
    def setUp(self):
        set_embedder_factory(lambda: _StaticEmbedder())

    def tearDown(self):
        set_embedder_factory(None)

    def test_initialize_lists_protocol_and_server_info(self):
        server = McpServer()
        resp = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        self.assertIsNotNone(resp)
        assert resp is not None
        self.assertEqual(resp["id"], 1)
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "skygrep")
        self.assertIn("tools", resp["result"]["capabilities"])

    def test_initialized_notification_returns_none(self):
        server = McpServer()
        resp = server.handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        self.assertIsNone(resp)

    def test_tools_list_includes_search_and_agent_context(self):
        server = McpServer()
        resp = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert resp is not None
        names = {tool["name"] for tool in resp["result"]["tools"]}
        self.assertEqual(names, {"search", "agent_context"})
        schemas = {t["name"]: t["inputSchema"] for t in resp["result"]["tools"]}
        self.assertIn("query", schemas["search"]["required"])
        self.assertEqual(schemas["agent_context"]["properties"]["top_k"]["default"], 8)

    def test_tools_call_search_delegates_to_storage_search(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = _seed_index(root)
            with patch.dict("os.environ", {"SKYGREP_DB_PATH": str(db_path)}, clear=False):
                server = McpServer(default_path=str(root))
                resp = server.handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "search",
                            "arguments": {
                                "query": "where is access token refresh?",
                                "path": str(root),
                                "top_k": 4,
                            },
                        },
                    }
                )
        assert resp is not None
        result = resp["result"]
        self.assertFalse(result["isError"])
        payload = result["structuredContent"]
        self.assertEqual(payload["tool"], "search")
        self.assertGreaterEqual(len(payload["results"]), 1)
        self.assertIn("token_refresh", payload["results"][0]["path"])

    def test_tools_call_agent_context_delegates_to_shared_pipeline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = _seed_index(root)
            with patch.dict("os.environ", {"SKYGREP_DB_PATH": str(db_path)}, clear=False):
                result = call_tool(
                    "agent_context",
                    {
                        "query": "where is access token refresh implemented?",
                        "path": str(root),
                        "top_k": 4,
                    },
                )
        self.assertFalse(result["isError"])
        payload = result["structuredContent"]
        self.assertEqual(payload["tool"], "agent_context")
        self.assertGreaterEqual(len(payload["results"]), 1)
        self.assertIn("token_refresh", payload["results"][0]["path"])
        # agent_context evidence fields from the shared pipeline
        self.assertTrue(
            payload["results"][0].get("candidate_recall")
            or "agent_summary" in payload["results"][0]
            or payload["results"][0].get("snippet")
        )

    def test_error_no_index(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            # Isolate from ambient SKYGREP_DB_PATH and use a missing DB path.
            missing_db = root / "definitely-missing.db"
            env = {"SKYGREP_DB_PATH": str(missing_db)}
            with patch.dict("os.environ", env, clear=False):
                result = call_tool(
                    "search",
                    {"query": "anything", "path": str(root)},
                )
            self.assertTrue(result["isError"], result)
            err = result["structuredContent"]
            self.assertEqual(err["error"], "no_index")

            # Also cover an existing but empty index.
            empty_db = root / "empty.db"
            init_db(empty_db).close()
            with patch.dict(
                "os.environ", {"SKYGREP_DB_PATH": str(empty_db)}, clear=False
            ):
                result2 = call_tool(
                    "agent_context",
                    {"query": "anything", "path": str(root)},
                )
            self.assertTrue(result2["isError"], result2)
            self.assertEqual(result2["structuredContent"]["error"], "no_index")

    def test_error_bad_path(self):
        result = call_tool(
            "search",
            {"query": "anything", "path": "/nonexistent/skygrep/mcp/path-xyz"},
        )
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"], "bad_path")

    def test_error_embedder_down(self):
        set_embedder_factory(lambda: _RaisingEmbedder())
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = _seed_index(root)
            with patch.dict("os.environ", {"SKYGREP_DB_PATH": str(db_path)}, clear=False):
                result = call_tool(
                    "search",
                    {"query": "token refresh", "path": str(root)},
                )
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"], "embedder_down")

    def test_stdio_framing_roundtrip(self):
        server = McpServer()
        request = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/list",
        }
        body = json.dumps(request)
        framed = f"Content-Length: {len(body.encode('utf-8'))}\r\n\r\n{body}"
        stdin = io.StringIO(framed)
        stdout = io.StringIO()

        # Drive a single message through serve_stdio then EOF.
        def _handle_once():
            msg = mcp_server._read_message(stdin)
            self.assertIsNotNone(msg)
            resp = server.handle(msg)  # type: ignore[arg-type]
            self.assertIsNotNone(resp)
            mcp_server._write_message(stdout, resp)  # type: ignore[arg-type]

        _handle_once()
        out = stdout.getvalue()
        self.assertIn("Content-Length:", out)
        # Parse body after headers
        _, raw_body = out.split("\r\n\r\n", 1)
        parsed = json.loads(raw_body)
        names = {t["name"] for t in parsed["result"]["tools"]}
        self.assertEqual(names, {"search", "agent_context"})

    def test_cli_mcp_help_lists_subcommand(self):
        runner = CliRunner()
        result = runner.invoke(cli_module.cli, ["mcp", "--help"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("MCP", result.output)
        lowered = result.output.lower()
        self.assertTrue("search" in lowered or "agent_context" in lowered or "stdio" in lowered)

    def test_mcp_tool_error_dict_shape(self):
        err = McpToolError("no_index", "missing")
        self.assertEqual(err.as_dict(), {"error": "no_index", "message": "missing"})


if __name__ == "__main__":
    unittest.main()
