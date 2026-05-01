import os
from pathlib import Path

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_EMBED_MODEL = "mxbai-embed-large"
DEFAULT_DB_PATH = Path.home() / ".local-mgrep" / "index.db"

def get_config():
    return {
        "ollama_url": os.environ.get("OLLAMA_URL", DEFAULT_OLLAMA_URL),
        "embed_model": os.environ.get("OLLAMA_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        "db_path": Path(os.environ.get("MGREP_DB_PATH", str(DEFAULT_DB_PATH))),
    }