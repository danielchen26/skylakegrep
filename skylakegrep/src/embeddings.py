# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
import os
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
    ``mxbai-embed-large`` / ``bge-m3``) get an empty prefix and behave as
    before.

    The default backend is Ollama. Set ``SKYGREP_EMBED_BACKEND=sentence-transformers``
    to run a HuggingFace-hosted model in-process instead. The ST backend
    is useful for ``bge-m3`` / ``bge-large-en-v1.5`` users without a
    local Ollama server, and for benchmarks where we want to compare a
    second backend without disturbing the Ollama keep-alive cache.
    """

    cfg = get_config()
    prefix = cfg["embed_prefixes"].get(role, "")
    backend = cfg.get("embed_backend", "ollama")
    if backend == "sentence-transformers":
        return SentenceTransformersEmbedder(
            cfg["embed_model"],
            prefix=prefix,
        )
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


def _timeout_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(0.5, float(raw))
    except ValueError:
        return default


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
        self.request_timeout_s: float | None = None
        self.batch_timeout_s: float | None = None
        self.allow_per_chunk_fallback: bool = True
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

    def _request_timeout(self, *, batch: bool) -> float:
        attr = self.batch_timeout_s if batch else self.request_timeout_s
        if attr is not None:
            return max(0.5, float(attr))
        if batch:
            return _timeout_from_env("SKYGREP_OLLAMA_BATCH_TIMEOUT_S", 120.0)
        return _timeout_from_env("SKYGREP_OLLAMA_TIMEOUT_S", 60.0)

    def embed(self, text: str) -> list[float]:
        try:
            resp = requests.post(
                f"{self.base_url}/api/embeddings",
                json=self._maybe_keep_alive(
                    {"model": self.model, "prompt": self._prep(text)}
                ),
                timeout=self._request_timeout(batch=False),
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
                timeout=self._request_timeout(batch=True),
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
            if not self.allow_per_chunk_fallback:
                logger.warning(
                    "foreground batch embed fallback disabled; substituting zero vectors"
                )
                return [self._zero_vector() for _ in texts]
        # Per-chunk fallback isolates failures to the offending chunk.
        return [self.embed(t) for t in texts]


# Map of skygrep model name → HuggingFace repo id, for the
# sentence-transformers backend. Keys here MUST match the bare model name
# (no Ollama tag) used in EMBED_PREFIXES, so prefix lookup keeps working.
_ST_MODEL_REGISTRY = {
    "bge-m3": "BAAI/bge-m3",
    "bge-large-en-v1.5": "BAAI/bge-large-en-v1.5",
    "nomic-embed-text-v1.5": "nomic-ai/nomic-embed-text-v1.5",
}


class SentenceTransformersEmbedder:
    """In-process embedding via ``sentence-transformers``.

    Drop-in replacement for ``OllamaEmbedder``: same ``embed`` /
    ``embed_batch`` contract, same prefix handling. We lazy-load the
    model on first ``embed`` call so that simply importing this module
    never pays the multi-hundred-MB model load cost.

    The class only handles the local-HF path; if ``sentence-transformers``
    is not installed we surface the install hint via an ImportError on
    first ``embed`` (not at import time, again to keep import cheap).
    """

    def __init__(self, model: str, prefix: str = ""):
        self.model_name = model
        self.prefix = prefix
        self._model = None
        self._zero_dim: int | None = None

    def _hf_repo(self) -> str:
        # Strip any Ollama tag (``bge-m3:latest`` → ``bge-m3``) before HF
        # lookup; ST consumers never use Ollama tags.
        bare = self.model_name.split(":", 1)[0]
        return _ST_MODEL_REGISTRY.get(bare, bare)

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise ImportError(
                    "SKYGREP_EMBED_BACKEND=sentence-transformers requires the "
                    "'sentence-transformers' package. Install with: "
                    "pip install sentence-transformers"
                ) from exc
            self._model = SentenceTransformer(self._hf_repo())
        return self._model

    def _prep(self, text: str) -> str:
        return f"{self.prefix}{_clip(text)}" if self.prefix else _clip(text)

    def _zero_vector(self) -> list[float]:
        if self._zero_dim is None:
            self._zero_dim = 1024  # bge-m3 / bge-large default; corrected on success
        return [0.0] * self._zero_dim

    def embed(self, text: str) -> list[float]:
        try:
            model = self._load()
            vec = model.encode(self._prep(text), normalize_embeddings=True)
            out = list(map(float, vec.tolist() if hasattr(vec, "tolist") else vec))
            if out:
                self._zero_dim = len(out)
            return out
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "ST embed failed for chunk of %d chars: %s; substituting zero vector",
                len(text),
                exc,
            )
            return self._zero_vector()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        prepped = [self._prep(t) for t in texts]
        try:
            model = self._load()
            arr = model.encode(prepped, normalize_embeddings=True, batch_size=32)
            out = [list(map(float, row.tolist() if hasattr(row, "tolist") else row)) for row in arr]
            if out and out[0]:
                self._zero_dim = len(out[0])
            return out
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "ST batch embed failed for %d chunks: %s; falling back to per-chunk",
                len(texts),
                exc,
            )
            return [self.embed(t) for t in texts]
