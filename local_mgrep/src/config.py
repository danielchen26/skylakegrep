import os
from pathlib import Path

DEFAULT_OLLAMA_URL = "http://localhost:11434"
# nomic-embed-text supersedes mxbai-embed-large as the local default. It is
# already in the recommended Ollama starter set, supports asymmetric query/
# document prefixes, and benchmarks slightly higher on code retrieval. Existing
# indexes built under mxbai-embed-large will trigger a dim-mismatch warning at
# search time and require ``mgrep index <repo> --reset``.
DEFAULT_EMBED_MODEL = "nomic-embed-text"
DEFAULT_LLM_MODEL = "qwen2.5:3b"
DEFAULT_DB_PATH = Path.home() / ".local-mgrep" / "index.db"

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
        "db_path": Path(os.environ.get("MGREP_DB_PATH", str(DEFAULT_DB_PATH))),
        "rerank_model": os.environ.get("MGREP_RERANK_MODEL", DEFAULT_RERANK_MODEL),
        "rerank_pool": int(os.environ.get("MGREP_RERANK_POOL", str(DEFAULT_RERANK_POOL))),
    }


def _strip_tag(model: str) -> str:
    return model.split(":", 1)[0]
