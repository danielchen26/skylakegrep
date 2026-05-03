import logging
import requests
from .answerer import _coerce_keep_alive
from .config import get_config

logger = logging.getLogger(__name__)

# Cap each input passed to Ollama. Common embedding models (mxbai-embed-large,
# nomic-embed-text) advertise a context length of 512 tokens which is roughly
# 2000 chars of code; some Ollama builds reject inputs above that length with
# a 400 instead of silently truncating server-side. We hard-cap inputs here so
# indexing a large repository never fails on a single oversized chunk.
MAX_INPUT_CHARS = 7500


def get_embedder(role: str = "document"):
    """Return an embedder configured for query or document side.

    Models like ``nomic-embed-text`` use asymmetric prefixes
    (``search_query:`` vs ``search_document:``); they are looked up from
    ``config.EMBED_PREFIXES``. Models without a documented prefix (e.g.
    ``mxbai-embed-large``) get an empty prefix and behave as before.
    """

    cfg = get_config()
    prefix = cfg["embed_prefixes"].get(role, "")
    return OllamaEmbedder(
        cfg["ollama_url"],
        cfg["embed_model"],
        prefix=prefix,
        keep_alive=cfg.get("keep_alive"),
    )


def _clip(text: str) -> str:
    if len(text) <= MAX_INPUT_CHARS:
        return text
    return text[:MAX_INPUT_CHARS]


class OllamaEmbedder:
    def __init__(
        self,
        base_url: str,
        model: str,
        prefix: str = "",
        keep_alive: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.prefix = prefix
        # ``keep_alive=-1`` keeps the embed model resident across calls,
        # which avoids 5-10 s reload latency between query embeddings in
        # the same shell session. None / empty string falls back to
        # Ollama's default (~5 min).
        self.keep_alive = keep_alive
        self._zero_dim: int | None = None

    def _prep(self, text: str) -> str:
        return f"{self.prefix}{_clip(text)}" if self.prefix else _clip(text)

    def _zero_vector(self) -> list[float]:
        if self._zero_dim is None:
            self._zero_dim = 768  # nomic-embed-text default; corrected on first success
        return [0.0] * self._zero_dim

    def _maybe_keep_alive(self, payload: dict) -> dict:
        ka = _coerce_keep_alive(self.keep_alive)
        if ka is not None:
            payload["keep_alive"] = ka
        return payload

    def embed(self, text: str) -> list[float]:
        try:
            resp = requests.post(
                f"{self.base_url}/api/embeddings",
                json=self._maybe_keep_alive(
                    {"model": self.model, "prompt": self._prep(text)}
                ),
                timeout=60,
            )
            resp.raise_for_status()
            vec = resp.json().get("embedding")
            if isinstance(vec, list) and vec:
                self._zero_dim = len(vec)
                return vec
        except requests.RequestException as exc:
            logger.warning(
                "embed failed for chunk of %d chars: %s; substituting zero vector",
                len(text),
                exc,
            )
        return self._zero_vector()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        prepped = [self._prep(t) for t in texts]
        try:
            resp = requests.post(
                f"{self.base_url}/api/embed",
                json=self._maybe_keep_alive(
                    {"model": self.model, "input": prepped}
                ),
                timeout=120,
            )
            resp.raise_for_status()
            embeddings = resp.json().get("embeddings")
            if isinstance(embeddings, list) and len(embeddings) == len(texts):
                if embeddings and embeddings[0]:
                    self._zero_dim = len(embeddings[0])
                return embeddings
        except requests.RequestException as exc:
            logger.warning(
                "batch embed failed for %d chunks: %s; falling back to per-chunk",
                len(texts),
                exc,
            )
        # Per-chunk fallback isolates failures to the offending chunk.
        return [self.embed(t) for t in texts]
