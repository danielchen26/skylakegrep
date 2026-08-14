"""Long-running daemon that holds the reranker + embedder warm.

The biggest single per-query cost in our pipeline is the cross-encoder
reranker cold load (~30 s on Mac CPU for ``mxbai-rerank-large-v2``). When a
short-lived ``skygrep search`` invocation pays this on every call the daily
driver tier is unusably slow. This module exposes a tiny HTTP server that
keeps the reranker and embedder loaded across requests; the CLI hands the
search to the daemon over localhost when the user has one running.

Stdlib only: ``http.server.ThreadingHTTPServer``. No FastAPI / Flask.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .config import get_config, project_root as resolve_project_root
from .embeddings import get_embedder
from .storage import search

logger = logging.getLogger(__name__)

DEFAULT_DAEMON_HOST = "127.0.0.1"
DEFAULT_DAEMON_PORT = 7878

# Single global lock around search since SQLite connections are not safe
# across threads, and the cross-encoder reranker is not thread-safe either.
# Throughput is single-query-at-a-time; we trade concurrency for simplicity.
_lock = threading.Lock()


class _SearchHandler(BaseHTTPRequestHandler):
    """Single endpoint: ``POST /search``.

    Request body (JSON): ``{"query": str, "top_k": int, "rerank": bool,
    "rerank_pool": int, "multi_resolution": bool, "file_top": int,
    "hyde": bool, "languages": [str], "include": [str], "exclude": [str],
    "snippet_chars": int, "agent_mode": str, "strict": bool}``.

    Response body (JSON): list of result dicts with the same shape the CLI's
    JSON output uses. Snippets are truncated to ``snippet_chars`` (default
    500) so a chatty agent doesn't pull a 50-MB response on every call.
    """

    server_version = "skylakegrep-daemon/0.3"

    def log_message(self, format, *args):  # noqa: A002 - parent signature
        # Keep daemon stdout tidy. Standard library default would print a
        # noisy access-log line per request.
        return

    def do_POST(self):
        if self.path != "/search":
            self.send_error(404, "Only POST /search is supported")
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            self.send_error(400, f"Invalid JSON: {exc}")
            return
        query = body.get("query")
        if not query:
            self.send_error(400, "query is required")
            return
        cfg = get_config()
        snippet_chars = int(body.get("snippet_chars", 500))
        configured_db = Path(cfg["db_path"]).expanduser().resolve()
        requested_db = Path(body.get("db_path") or configured_db).expanduser().resolve()
        configured_root = resolve_project_root().expanduser().resolve()
        requested_root = Path(
            body.get("project_root") or configured_root
        ).expanduser().resolve()
        lexical_root = Path(
            body.get("lexical_root") or requested_root
        ).expanduser().resolve()
        try:
            lexical_root.relative_to(configured_root)
            lexical_root_allowed = True
        except ValueError:
            lexical_root_allowed = False
        if requested_db != configured_db or requested_root != configured_root:
            self.send_error(409, "daemon project/index does not match the client request")
            return
        if not lexical_root_allowed:
            self.send_error(400, "lexical_root must stay inside the daemon project root")
            return
        # Optional HyDE expansion runs in-thread because it's just an HTTP
        # call to the local Ollama LLM and the LLM is not held by us.
        embed_input = query
        if body.get("hyde"):
            try:
                from .answerer import get_answerer

                embed_input = get_answerer().hyde(query)
            except Exception as exc:  # pragma: no cover - best effort
                logger.warning("HyDE expansion failed: %s", exc)
        with _lock, closing(sqlite3.connect(configured_db)) as conn:
            embedder = get_embedder(role="query")
            started = time.perf_counter()
            agent_mode = str(body.get("agent_mode") or "off")
            if agent_mode == "context":
                from .candidate_recall import run_agent_context_search

                results, _ = run_agent_context_search(
                    conn,
                    str(body.get("recall_query") or query),
                    lexical_root,
                    top_k=int(body.get("top_k", 8)),
                    languages=tuple(body.get("languages", []) or []),
                    include_patterns=tuple(body.get("include", []) or []),
                    exclude_patterns=tuple(body.get("exclude", []) or []),
                    max_paths=max(int(body.get("top_k", 8)) * 8, int(body.get("top_k", 8))),
                    rg_timeout=float(body.get("rg_timeout", 0.75)),
                    support_per_path=int(body.get("support_per_path", 2)),
                    semantic_only=bool(body.get("semantic_only", False)),
                    strict=bool(body.get("strict", False)),
                    embedder_factory=lambda: embedder,
                    source_root=configured_root,
                )
            else:
                results = search(
                    conn,
                    embedder.embed(embed_input),
                    top_k=int(body.get("top_k", 10)),
                    languages=tuple(body.get("languages", []) or []),
                    include_patterns=tuple(body.get("include", []) or []),
                    exclude_patterns=tuple(body.get("exclude", []) or []),
                    query_text=query,
                    rerank=bool(body.get("rerank", True)),
                    rerank_pool=int(body.get("rerank_pool", cfg["rerank_pool"])),
                    rerank_model=body.get("rerank_model"),
                    multi_resolution=bool(body.get("multi_resolution", True)),
                    file_top=int(body.get("file_top", 30)),
                )
            latency = time.perf_counter() - started
        optional_keys = (
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
        )
        serialized_results = []
        for result in results:
            item = {
                "path": result["path"],
                "start_line": result.get("start_line"),
                "end_line": result.get("end_line"),
                "language": result.get("language"),
                "score": float(result["score"]),
                "snippet": (result.get("snippet") or "")[:snippet_chars],
            }
            if agent_mode == "context":
                for key in optional_keys:
                    if key in result:
                        item[key] = result[key]
            serialized_results.append(item)
        payload = {
            "results": serialized_results,
            "latency_seconds": round(latency, 4),
        }
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _warm_reranker() -> None:
    """Best-effort reranker warmup for an already-listening daemon.

    Importing ``sentence_transformers`` also imports PyTorch and may take a
    long time (or block on a broken optional installation). Keep that work
    out of the daemon readiness path so optional acceleration cannot prevent
    lightweight no-rerank requests from being served.
    """

    try:
        from .reranker import get_reranker

        reranker = get_reranker()
        if reranker is not None:
            reranker.score("warmup", ["x"])
            logger.info("reranker pre-warmed")
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("reranker warmup failed: %s", exc)


def _start_reranker_warmup() -> threading.Thread:
    """Start optional reranker warmup without delaying daemon readiness."""

    thread = threading.Thread(
        target=_warm_reranker,
        name="skygrep-reranker-warmup",
        daemon=True,
    )
    thread.start()
    return thread


def serve(
    host: str = DEFAULT_DAEMON_HOST,
    port: int = DEFAULT_DAEMON_PORT,
    *,
    warm_reranker: bool = False,
) -> None:
    """Block forever, serving search requests on ``host:port``.

    Readiness never depends on optional ML imports. Callers may explicitly
    request a background reranker warmup; the default serves agent presets
    immediately and lets reranked requests load lazily.
    """

    cfg = get_config()
    db_path: Path = cfg["db_path"]
    if not db_path.exists():
        logger.warning(
            "DB not found at %s — start the daemon after running `skygrep index <path>`",
            db_path,
        )
    server = ThreadingHTTPServer((host, port), _SearchHandler)
    logger.info("skylakegrep daemon ready at http://%s:%d", host, port)
    print(f"skylakegrep daemon ready at http://{host}:{port}", flush=True)
    if warm_reranker:
        _start_reranker_warmup()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()


def daemon_search(
    base_url: str,
    query: str,
    *,
    top_k: int = 10,
    rerank: bool = True,
    rerank_pool: Optional[int] = None,
    multi_resolution: bool = True,
    file_top: int = 30,
    hyde: bool = False,
    languages: tuple[str, ...] = (),
    include_patterns: tuple[str, ...] = (),
    exclude_patterns: tuple[str, ...] = (),
    agent_mode: str = "off",
    strict: bool = False,
    semantic_only: bool = False,
    project_root: str | None = None,
    db_path: str | None = None,
    recall_query: str | None = None,
    lexical_root: str | None = None,
    rg_timeout: float = 0.75,
    support_per_path: int = 2,
    snippet_chars: int = 500,
    timeout: float = 120.0,
) -> dict:
    """Client helper: POST a search to a running daemon and return its JSON.

    Used by the CLI's ``--daemon-url`` path and by benchmarks that want to
    measure warm-cache latency. Raises ``requests.RequestException`` on
    network failure so the caller can fall back to in-process search.
    """

    import requests  # local import keeps server-side import-time small

    payload = {
        "query": query,
        "top_k": top_k,
        "rerank": rerank,
        "rerank_pool": rerank_pool,
        "multi_resolution": multi_resolution,
        "file_top": file_top,
        "hyde": hyde,
        "languages": list(languages),
        "include": list(include_patterns),
        "exclude": list(exclude_patterns),
        "agent_mode": agent_mode,
        "strict": strict,
        "semantic_only": semantic_only,
        "project_root": project_root,
        "db_path": db_path,
        "recall_query": recall_query,
        "lexical_root": lexical_root,
        "rg_timeout": rg_timeout,
        "support_per_path": support_per_path,
        "snippet_chars": snippet_chars,
    }
    response = requests.post(f"{base_url.rstrip('/')}/search", json=payload, timeout=timeout)
    response.raise_for_status()
    return response.json()
