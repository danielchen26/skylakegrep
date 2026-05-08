from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

DEFAULT_OLLAMA_URL = "http://localhost:11434"
# bge-m3 is BAAI's flagship "multi-functional, multi-lingual,
# multi-granularity" embedding model — a content-agnostic 1024-d general
# purpose embedder that handles code, natural-language docs, and config
# uniformly in the same vector space. It supersedes both nomic-embed-text
# (which biased toward re-export aggregators on vocabulary-mismatch
# queries because of its asymmetric search_query/search_document
# prefixes designed for retrieval over web text) and mxbai-embed-large
# (which ranked aggregator ``packages/react/index.js`` files above the
# canonical implementation in ``packages/react/src/jsx/...``). bge-m3
# is symmetric (no prefix needed), uses the XLM-RoBERTa backbone with
# code-aware pretraining, and matches or beats both predecessors on
# vocabulary-mismatch retrieval.
#
# Existing indexes built under ``nomic-embed-text`` (768-d) or
# ``mxbai-embed-large`` (1024-d) will trigger a dim-mismatch warning at
# search time and require ``skygrep index <repo> --reset``.
#
# Override via ``OLLAMA_EMBED_MODEL`` to revert to ``nomic-embed-text``,
# ``mxbai-embed-large`` or any other Ollama-served embedder.
DEFAULT_EMBED_MODEL = "bge-m3"
DEFAULT_LLM_MODEL = "qwen2.5:3b"
# HyDE / cascade-escalation default. We keep the same 3B model used by
# ``--answer`` because empirical 16-task Rust benchmarking showed that
# smaller (qwen2.5:1.5b) drops recall by 1 task (the
# ``app/src/command_palette.rs`` query: HyDE-generated keystroke /
# command-palette identifiers are less plausible from the smaller
# model). Users who prefer faster cascade-escalations can set
# ``OLLAMA_HYDE_MODEL=qwen2.5:1.5b`` explicitly: ~30 % per-query
# speedup at the cost of one task on Rust workspace.
DEFAULT_HYDE_MODEL = "qwen2.5:3b"
# Ollama keep-alive: -1 keeps a model resident indefinitely after the first
# load, which is what we want for an interactive CLI — the next query in
# the same shell session no longer pays a 5-10 s cold-load. Override with a
# duration string (``"30m"``, ``"60s"``) or ``"0"`` to disable.
DEFAULT_KEEP_ALIVE = "-1"
GLOBAL_INDEX_FALLBACK = Path.home() / ".skylakegrep" / "index.db"
PROJECT_INDEX_DIR = Path.home() / ".skylakegrep" / "repos"


def project_root(start: Path | None = None) -> Path:
    """Resolve the directory we should treat as 'this project'.

    Strategy: ask ``git rev-parse --show-toplevel`` from ``start`` (default
    cwd). If that succeeds, return the git root. Otherwise return ``start``
    itself — non-git directories still get a per-directory index.
    """

    base = (start or Path.cwd()).resolve()
    try:
        out = subprocess.run(
            ["git", "-C", str(base), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if out.returncode == 0:
            top = out.stdout.strip()
            if top:
                return Path(top).resolve()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return base


def project_db_path(root: Path | None = None) -> Path:
    """Derive a per-project SQLite DB path from the project root.

    Each project gets its own DB at ``~/.skylakegrep/repos/<basename>-<8-hex>.db``
    where ``8-hex`` is the first 8 chars of the SHA-256 of the absolute root
    path. Collisions across two projects with the same basename are
    impossible because the path hash distinguishes them.
    """

    root = (root or project_root()).resolve()
    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    name = root.name or "root"
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)
    return PROJECT_INDEX_DIR / f"{safe}-{digest}.db"


def resolve_db_path(explicit: str | None = None) -> Path:
    """Resolve the SQLite path to use for this invocation.

    Precedence: explicit argument → ``SKYGREP_DB_PATH`` env override → derived
    project-scoped path. Older versions defaulted to a single global file
    (``~/.skylakegrep/index.db``). Callers that want the legacy behaviour
    can set ``SKYGREP_DB_PATH`` to that path explicitly.
    """

    if explicit:
        return Path(explicit)
    env = os.environ.get("SKYGREP_DB_PATH")
    if env:
        return Path(env)
    return project_db_path()


# Compatibility alias for callers still importing the constant.
DEFAULT_DB_PATH = GLOBAL_INDEX_FALLBACK

# Cross-encoder reranker (optional dep ``sentence-transformers``).
# ``mxbai-rerank-large-v2`` is Mixedbread's flagship reranker and the model
# their cloud product uses internally. It is ~3× larger than the base variant
# (568M vs 184M parameters, ~1.2GB on disk vs ~370MB) but lifts recall on
# code-search benchmarks measurably and is what we need to match the cloud
# product's accuracy. Override with ``SKYGREP_RERANK_MODEL`` if disk space or
# CPU budget is tight.
DEFAULT_RERANK_MODEL = "mixedbread-ai/mxbai-rerank-large-v2"
DEFAULT_RERANK_POOL = 50

# Asymmetric prefixes per embedding model. Empty string means the model
# does not document a query/document distinction; we leave the input as-is.
# bge-m3 is symmetric out of the box (no query/document prefix recommended
# by BAAI). bge-large-en-v1.5 documents an optional query prefix
# ("Represent this sentence for searching relevant passages: ") which we
# carry only if a user explicitly opts into bge-large-en-v1.5 via the
# OLLAMA_EMBED_MODEL override.
EMBED_PREFIXES = {
    "bge-m3": {"query": "", "document": ""},
    "bge-large-en-v1.5": {
        "query": "Represent this sentence for searching relevant passages: ",
        "document": "",
    },
    "nomic-embed-text": {
        "query": "search_query: ",
        "document": "search_document: ",
    },
    "nomic-embed-text-v1.5": {
        "query": "search_query: ",
        "document": "search_document: ",
    },
    "mxbai-embed-large": {"query": "", "document": ""},
}


def get_config():
    embed_model = os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL)
    # Embedding backend: ``ollama`` (default, requires local Ollama
    # runtime) or ``sentence-transformers`` (in-process, no Ollama).
    # The Ollama path is what the rest of the project uses; the ST path
    # exists so users without Ollama can still run skygrep with
    # bge-m3 / bge-large-en-v1.5 directly off Hugging Face.
    embed_backend = os.environ.get("SKYGREP_EMBED_BACKEND", "ollama").lower()
    return {
        "ollama_url": os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
        "embed_model": embed_model,
        "embed_backend": embed_backend,
        "embed_prefixes": EMBED_PREFIXES.get(
            _strip_tag(embed_model), {"query": "", "document": ""}
        ),
        "llm_model": os.environ.get("OLLAMA_LLM_MODEL", DEFAULT_LLM_MODEL),
        "hyde_model": os.environ.get("OLLAMA_HYDE_MODEL", DEFAULT_HYDE_MODEL),
        "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", DEFAULT_KEEP_ALIVE),
        "db_path": resolve_db_path(),
        "rerank_model": os.environ.get("SKYGREP_RERANK_MODEL", DEFAULT_RERANK_MODEL),
        "rerank_pool": int(os.environ.get("SKYGREP_RERANK_POOL", str(DEFAULT_RERANK_POOL))),
    }


def _strip_tag(model: str) -> str:
    return model.split(":", 1)[0]
