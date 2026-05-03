"""Layer L3 — doc2query chunk enrichment.

For every chunk in the index that hasn't been enriched yet:

  1. Ask the local Ollama LLM for a 1-2 sentence high-level description of
     the chunk, focused on user-facing concepts ("auth", "billing",
     "language model backend") rather than implementation detail.
  2. Append the description to the chunk text.
  3. Re-compute the chunk embedding over the augmented text.
  4. Persist the description, an enrichment timestamp, and the rewritten
     embedding into the existing ``chunks`` and ``vectors`` tables.

The whole loop is structured to be **resumable**: each chunk is committed
on its own (commit-per-chunk), and the next call selects only chunks
where ``enriched_at IS NULL``, so a SIGINT mid-pass leaves the index in a
consistent state and the next ``mgrep enrich`` invocation picks up where
the last one left off.

This module deliberately mirrors the prompting / request shape used by
``answerer.OllamaAnswerer.hyde`` — same base URL, same model from
``get_config()['llm_model']``, same deterministic ``options`` dict
(``temperature: 0`` and ``seed: 42``) — so HyDE-style determinism extends
to enrichment as well.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

import numpy as np
import requests

from .config import get_config
from .embeddings import get_embedder

logger = logging.getLogger(__name__)


# Prompt template. Output is constrained to a 1-2 sentence high-level
# description so the augmented chunk text doesn't grow much (the LLM never
# emits code, never emits markdown, never asks follow-ups). This is the
# doc2query primitive: every chunk picks up a short natural-language tag
# whose tokens land in the embedding alongside the original code, which
# helps natural-language queries hit the right chunk via the embedding
# rather than relying purely on identifier overlap.
PROMPT_TEMPLATE = (
    "Write a one or two sentence high-level description of what this code\n"
    "does, focusing on user-facing concepts (e.g. \"auth\", \"billing\",\n"
    "\"language model backend\") rather than implementation detail. Output\n"
    "only the description, no preamble, no markdown.\n\n"
    "File: {path}\n"
    "{language_line}\n"
    "```{language}\n"
    "{chunk}\n"
    "```\n"
)


class _DefaultAnswerer:
    """Minimal Ollama wrapper for doc2query.

    We don't reuse ``OllamaAnswerer.hyde`` because the prompt and the
    failure mode differ: HyDE returns the original query on failure (so
    retrieval keeps going), while ``describe_chunk`` returns ``None`` so
    ``enrich_pending_chunks`` can skip the chunk and keep its
    ``enriched_at`` NULL — which is what makes the operation resumable.
    """

    def __init__(self, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def describe_chunk(self, path: str, language: str, chunk: str) -> Optional[str]:
        language = language or ""
        language_line = f"Language: {language}" if language else ""
        prompt = PROMPT_TEMPLATE.format(
            path=path,
            language=language,
            language_line=language_line,
            chunk=chunk,
        )
        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0, "seed": 42, "num_predict": 96},
                },
                timeout=120,
            )
            response.raise_for_status()
            text = (response.json().get("response") or "").strip()
            return text or None
        except requests.RequestException as exc:
            logger.warning("doc2query LLM call failed for %s: %s", path, exc)
            return None


def _default_answerer() -> _DefaultAnswerer:
    cfg = get_config()
    return _DefaultAnswerer(cfg["ollama_url"], cfg["llm_model"])


def count_pending(conn: sqlite3.Connection) -> int:
    """Number of chunks still awaiting enrichment."""
    row = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE enriched_at IS NULL"
    ).fetchone()
    return int(row[0]) if row else 0


def count_enriched(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return ``(enriched, total)`` chunk counts for status reporting."""
    row = conn.execute(
        "SELECT SUM(CASE WHEN enriched_at IS NOT NULL THEN 1 ELSE 0 END), "
        "COUNT(*) FROM chunks"
    ).fetchone()
    enriched = int(row[0]) if row and row[0] is not None else 0
    total = int(row[1]) if row and row[1] is not None else 0
    return enriched, total


def enrich_pending_chunks(
    conn: sqlite3.Connection,
    *,
    embedder=None,
    answerer=None,
    batch_size: int = 5,
    max_chunks: Optional[int] = None,
    quiet: bool = False,
) -> int:
    """Enrich chunks where ``enriched_at IS NULL``.

    For each candidate chunk:
      * Ask the local Ollama LLM for a 1-2 sentence description.
      * Append the description to the chunk text.
      * Re-embed the augmented text.
      * Update ``chunks.description``, ``chunks.enriched_at``, and the
        corresponding ``vectors.embedding`` row.
      * Commit per chunk so a kill mid-pass leaves a resumable state.

    Returns the number of chunks successfully enriched. When
    ``max_chunks`` is set the loop stops after that many successes;
    chunks where the LLM call failed are skipped (no commit) and remain
    candidates on the next invocation.

    Progress lines (one per ``batch_size`` enrichments) go to stderr only
    when ``quiet`` is False, mirroring ``auto_index.first_time_index``.
    """

    if embedder is None:
        embedder = get_embedder(role="document")
    if answerer is None:
        answerer = _default_answerer()

    cursor = conn.execute(
        "SELECT id, file, chunk, language FROM chunks WHERE enriched_at IS NULL "
        "ORDER BY id"
    )
    enriched_n = 0
    t0 = time.time()
    for row in cursor:
        if max_chunks is not None and enriched_n >= max_chunks:
            break
        chunk_id, path, chunk_text, language = row
        chunk_text = chunk_text or ""
        # Hand off to the LLM. ``describe_chunk`` returns None on transport
        # failure so we leave ``enriched_at`` NULL and the chunk stays a
        # candidate for the next run.
        description = answerer.describe_chunk(path, language or "", chunk_text)
        if not description:
            continue

        augmented = f"{chunk_text}\n\n{description}"
        new_embedding = embedder.embed(augmented)
        vec = np.array(new_embedding, dtype=np.float32)

        # Atomic per-chunk update: writing to chunks + vectors + commit
        # together makes the resume primitive correct — a chunk is either
        # fully enriched (description, timestamp, new vector) or not at
        # all.
        now = time.time()
        conn.execute(
            "UPDATE chunks SET description = ?, enriched_at = ? WHERE id = ?",
            (description, now, chunk_id),
        )
        conn.execute(
            "UPDATE vectors SET embedding = ? WHERE id = ?",
            (vec.tobytes(), chunk_id),
        )
        conn.commit()

        enriched_n += 1
        if not quiet and enriched_n % max(1, batch_size) == 0:
            elapsed = time.time() - t0
            try:
                import click

                click.echo(
                    f"  · {enriched_n} chunks enriched · {elapsed:.1f}s",
                    err=True,
                )
            except Exception:
                pass

    return enriched_n
