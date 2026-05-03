import hashlib
import os
import subprocess
from pathlib import Path

DEFAULT_OLLAMA_URL = "http://localhost:11434"
# nomic-embed-text supersedes mxbai-embed-large as the local default. It is
# already in the recommended Ollama starter set, supports asymmetric query/
# document prefixes, and benchmarks slightly higher on code retrieval. Existing
# indexes built under mxbai-embed-large will trigger a dim-mismatch warning at
# search time and require ``mgrep index <repo> --reset``.
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_LLM_MODEL = "qwen2.5:3b"
# HyDE / cascade-escalation use a smaller model by default — the LLM job is
# "write a short plausible code snippet matching the question's intent",
# which a 1.5B-class model handles competently and 3-5× faster on Mac CPU
# than the 3B used for ``--answer``. Override with ``OLLAMA_HYDE_MODEL`` to
# pin a specific model. If the configured model is missing locally the
# answerer falls back transparently to ``llm_model``.
DEFAULT_HYDE_MODEL = "qwen2.5:1.5b"
# Ollama keep-alive: -1 keeps a model resident indefinitely after the first
# load, which is what we want for an interactive CLI — the next query in
# the same shell session no longer pays a 5-10 s cold-load. Override with a
# duration string (``"30m"``, ``"60s"``) or ``"0"`` to disable.
DEFAULT_KEEP_ALIVE = "-1"
GLOBAL_INDEX_FALLBACK = Path.home() / ".local-mgrep" / "index.db"
PROJECT_INDEX_DIR = Path.home() / ".local-mgrep" / "repos"


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

    Each project gets its own DB at ``~/.local-mgrep/repos/<basename>-<8-hex>.db``
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

    Precedence: explicit argument → ``MGREP_DB_PATH`` env override → derived
    project-scoped path. Older versions defaulted to a single global file
    (``~/.local-mgrep/index.db``). Callers that want the legacy behaviour
    can set ``MGREP_DB_PATH`` to that path explicitly.
    """

    if explicit:
        return Path(explicit)
    env = os.environ.get("MGREP_DB_PATH")
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
# product's accuracy. Override with ``MGREP_RERANK_MODEL`` if disk space or
# CPU budget is tight.
DEFAULT_RERANK_MODEL = "mixedbread-ai/mxbai-rerank-large-v2"
DEFAULT_RERANK_POOL = 50

# Asymmetric prefixes per embedding model. Empty string means the model
# does not document a query/document distinction; we leave the input as-is.
EMBED_PREFIXES = {
    "nomic-embed-text": {
        "query": "search_query: ",
        "document": "search_document: ",
    },
    "mxbai-embed-large": {"query": "", "document": ""},
}


def get_config():
    embed_model = os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL)
    return {
        "ollama_url": os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
        "embed_model": embed_model,
        "embed_prefixes": EMBED_PREFIXES.get(
            _strip_tag(embed_model), {"query": "", "document": ""}
        ),
        "llm_model": os.environ.get("OLLAMA_LLM_MODEL", DEFAULT_LLM_MODEL),
        "hyde_model": os.environ.get("OLLAMA_HYDE_MODEL", DEFAULT_HYDE_MODEL),
        "keep_alive": os.environ.get("OLLAMA_KEEP_ALIVE", DEFAULT_KEEP_ALIVE),
        "db_path": resolve_db_path(),
        "rerank_model": os.environ.get("MGREP_RERANK_MODEL", DEFAULT_RERANK_MODEL),
        "rerank_pool": int(os.environ.get("MGREP_RERANK_POOL", str(DEFAULT_RERANK_POOL))),
    }


def _strip_tag(model: str) -> str:
    return model.split(":", 1)[0]
