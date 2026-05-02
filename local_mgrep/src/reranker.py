"""Cross-encoder reranker as a second-stage scorer.

A retrieval pipeline with cosine similarity alone is good at separating
"obviously relevant" from "obviously irrelevant" but poor at distinguishing
"highly relevant" from "top-1 relevant". A small cross-encoder reranker scoring
(query, chunk) pairs typically lifts top-k recall on code corpora by 10-30
points at a small latency cost.

This module is intentionally a thin wrapper. The heavy ML dep
(sentence-transformers + torch) is optional and lazily imported, so a base
``mgrep`` install stays light. Callers ask for the singleton via
``get_reranker()`` and either get a working reranker or ``None``; ``None``
should be treated as "skip rerank, use upstream cosine ordering".
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_RERANK_MODEL = os.environ.get(
    "MGREP_RERANK_MODEL", "mixedbread-ai/mxbai-rerank-large-v2"
)
DEFAULT_RERANK_POOL = int(os.environ.get("MGREP_RERANK_POOL", "50"))


_singleton: Optional["CrossEncoderReranker"] = None
_warned_missing = False


class CrossEncoderReranker:
    """Lazy wrapper around ``sentence_transformers.CrossEncoder``.

    The model is loaded on first ``score()`` call so that constructing the
    object is free. ``predict()`` is sequence-aware: an empty input list short-
    circuits to an empty output, so callers can pass the result of a filtered
    cosine search without checking length.
    """

    def __init__(self, model_name: str = DEFAULT_RERANK_MODEL):
        self.model_name = model_name
        self._model = None  # populated on first use

    def _load(self):
        if self._model is not None:
            return self._model
        # Lazy import to keep base install light; raise a clear error if missing.
        from sentence_transformers import CrossEncoder  # type: ignore

        logger.info("loading cross-encoder reranker: %s", self.model_name)
        self._model = CrossEncoder(self.model_name)
        return self._model

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        model = self._load()
        pairs = [(query, p) for p in passages]
        # ``predict`` returns numpy.float32 array; cast to Python floats so the
        # scores compose cleanly with the rest of our pipeline.
        scores = model.predict(pairs)
        return [float(s) for s in scores]


def get_reranker(model_name: Optional[str] = None) -> Optional[CrossEncoderReranker]:
    """Return a singleton reranker, or ``None`` if the optional dep is missing.

    First call attempts the lazy import once and caches the outcome; subsequent
    calls are O(1). Missing dep is logged once at WARNING; all later calls are
    silent.
    """

    global _singleton, _warned_missing
    target = model_name or DEFAULT_RERANK_MODEL
    if _singleton is not None and _singleton.model_name == target:
        return _singleton
    try:
        import sentence_transformers  # noqa: F401
    except ImportError:
        if not _warned_missing:
            logger.warning(
                "sentence-transformers not installed; skipping cross-encoder "
                "rerank. Install with: pip install 'local-mgrep[rerank]'"
            )
            _warned_missing = True
        return None
    _singleton = CrossEncoderReranker(model_name=target)
    return _singleton


def rerank(
    query: str,
    candidates: list[dict],
    *,
    text_key: str = "snippet",
    score_key: str = "rerank_score",
    fallback_score_key: str = "score",
    pool: Optional[int] = None,
    top_k: Optional[int] = None,
    model_name: Optional[str] = None,
) -> list[dict]:
    """Rerank ``candidates`` by cross-encoder score.

    The candidate list is truncated to ``pool`` (default 50) before reranking
    to bound latency, then the top ``top_k`` (default: full reranked list) is
    returned. When the optional dep is missing, returns the input unchanged so
    callers can wire this in unconditionally.
    """

    if not candidates:
        return []
    pool_size = pool if pool is not None else DEFAULT_RERANK_POOL
    pool_slice = candidates[: max(1, pool_size)]
    reranker_obj = get_reranker(model_name)
    if reranker_obj is None:
        # Graceful fallback: keep cosine ordering.
        for c in pool_slice:
            c.setdefault(score_key, c.get(fallback_score_key, 0.0))
        if top_k is None:
            return pool_slice + candidates[len(pool_slice):]
        return (pool_slice + candidates[len(pool_slice):])[:top_k]
    passages = [c.get(text_key, "") for c in pool_slice]
    scores = reranker_obj.score(query, passages)
    for c, s in zip(pool_slice, scores):
        c[score_key] = s
        c["score"] = s  # promote rerank score to the public ranking score
    pool_slice.sort(key=lambda c: c[score_key], reverse=True)
    if top_k is None:
        return pool_slice
    return pool_slice[: top_k]
