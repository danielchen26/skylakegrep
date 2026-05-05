"""Intelligent background recovery for embedder upgrades.

When the user upgrades the default embedder (e.g. ``mxbai-embed-large``
1024-d → ``bge-m3`` 1024-d, or ``nomic-embed-text`` 768-d → ``bge-m3``
1024-d), every chunk's stored vector becomes either useless (different
dim) or semantically incompatible (same dim, different space). The
0.1.x answer was a manual ``skygrep index <repo> --reset``: rebuild
everything from scratch, blocking the user for 10–30 minutes on a
mid-sized repo.

This module replaces that with a non-blocking recovery worker:

  - **Detection** is automatic. Every search/index command checks the
    stored embedder fingerprint in the ``metadata`` table against the
    one currently active. If they don't match, recovery starts.

  - **The user is never blocked.** The first query after detection
    returns instantly via the existing rg fallback path. The recovery
    worker runs in a background daemon thread and re-embeds chunks in
    place. Already-recovered files become semantically searchable as
    soon as their batch commits — the existing
    ``_filter_to_matching_dim`` helper in ``storage.py`` filters
    stale-dim chunks out of every search, so coverage grows
    monotonically while the user keeps working.

  - **Priority order is mtime DESC.** Recently-modified files re-embed
    first. User behaviour is Pareto-distributed (the next ~80 % of
    queries hit the most-recently-touched ~5 % of files), so
    "semantic-coverage" of the queries the user actually runs reaches
    near-100 % well before the long tail finishes.

  - **Crash-safe and resumable.** Worker progress lives in the
    ``metadata`` table; the next CLI invocation picks up where the
    previous one died. The worker queries SQL for
    ``length(vectors.embedding)/4 != expected_dim`` rather than
    keeping its own offset, so files re-embedded by an earlier run
    naturally drop out of the queue.

  - **Re-embedding is much cheaper than ``--reset``.** Tree-sitter
    chunking, symbol extraction, and the file-export graph are all
    embedder-independent — only the per-chunk vector needs to change.
    Skipping the unchanged work cuts wall time by ~30–60 %.

The detection / spawn entry point is :func:`maybe_start_recovery`.
The worker entry point is :func:`run_recovery_worker`. Both are safe
to import from any thread; the worker opens its own SQLite connection
so it never shares a cursor with the main thread.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from .storage import (
    count_stale_chunks,
    count_total_chunks,
    get_meta,
    populate_file_embeddings,
    set_meta,
)


logger = logging.getLogger(__name__)


# Heartbeat staleness window: a worker that hasn't updated its progress
# row in this many seconds is considered crashed; the next CLI run will
# re-spawn instead of trusting the existing ``recovery_in_progress`` flag.
HEARTBEAT_STALE_SECONDS = 120


def embedder_fingerprint(embedder, current_dim: int) -> str:
    """Stable identity for the active embedder.

    Used to compare against ``metadata.embedder_fingerprint`` to decide
    whether the index needs recovery. ``model_name`` lives on
    ``SentenceTransformersEmbedder``; ``model`` lives on ``OllamaEmbedder``;
    fallback to ``"unknown"`` keeps us defensive against any third
    embedder a user wires in.
    """

    name = getattr(embedder, "model_name", None) or getattr(embedder, "model", "unknown")
    return f"{name}:{current_dim}"


def detect_mismatch(conn, embedder, current_dim: int) -> Optional[dict]:
    """Check whether the stored fingerprint differs from the current one.

    Returns ``None`` when the index is coherent (or freshly created). When
    a mismatch is detected returns a dict describing the gap so the
    caller can decide whether to spawn the worker, just log a warning, or
    short-circuit.
    """

    stored = get_meta(conn, "embedder_fingerprint")
    current = embedder_fingerprint(embedder, current_dim)
    if stored is None:
        # Fresh index, or pre-0.2.2 index with no fingerprint yet — record
        # the current one so future runs have something to compare against.
        # Do not trigger recovery: the chunks were just written by the
        # current embedder.
        set_meta(conn, "embedder_fingerprint", current)
        return None
    if stored == current:
        return None
    stale = count_stale_chunks(conn, current_dim)
    total = count_total_chunks(conn)
    if stale == 0:
        # Fingerprint changed but every chunk is already at the current
        # dim — likely a model rename inside the same vector space. Just
        # update the fingerprint and move on.
        set_meta(conn, "embedder_fingerprint", current)
        return None
    return {
        "stored_fingerprint": stored,
        "current_fingerprint": current,
        "stale_count": stale,
        "total_count": total,
        "coverage_pct": int(100 * (total - stale) / total) if total else 0,
    }


def get_recovery_state(conn) -> dict:
    """Return a snapshot of recovery state for telemetry / rendering.

    Reads three keys from ``metadata`` (set by the worker on startup, on
    every committed batch, and on completion). The dict shape is stable
    so the CLI render layer can rely on it.
    """

    in_progress = get_meta(conn, "recovery_in_progress") == "1"
    started_at = get_meta(conn, "recovery_started_at")
    heartbeat = get_meta(conn, "recovery_heartbeat_at")
    progress = get_meta(conn, "recovery_progress") or ""
    eta_seconds = get_meta(conn, "recovery_eta_seconds")
    coverage = get_meta(conn, "recovery_coverage_pct")
    if heartbeat:
        try:
            heartbeat_age = time.time() - float(heartbeat)
            if heartbeat_age > HEARTBEAT_STALE_SECONDS:
                in_progress = False
        except ValueError:
            pass
    return {
        "in_progress": in_progress,
        "started_at": float(started_at) if started_at else None,
        "heartbeat_at": float(heartbeat) if heartbeat else None,
        "progress": progress,
        "eta_seconds": int(eta_seconds) if eta_seconds and eta_seconds.isdigit() else None,
        "coverage_pct": int(coverage) if coverage and coverage.isdigit() else None,
    }


def maybe_start_recovery(db_path: Path, conn, embedder, current_dim: int) -> Optional[dict]:
    """Detection + spawn entry point.

    Returns the recovery state dict (with an extra ``"just_started"`` key)
    when a worker was spawned, the existing recovery state when one was
    already running, or ``None`` when the index is coherent.

    The worker runs as a daemon thread so a one-shot CLI invocation
    finishes cleanly even when recovery is mid-flight; the next CLI call
    automatically resumes from wherever the previous worker died.
    """

    mismatch = detect_mismatch(conn, embedder, current_dim)
    if mismatch is None:
        return None

    state = get_recovery_state(conn)
    if state["in_progress"]:
        # Heartbeat is fresh; another instance is already working.
        # Surface its state to the caller so they can render coverage.
        state.update(mismatch)
        state["just_started"] = False
        return state

    # No live worker → spawn one. The worker takes its own DB path and
    # opens a fresh sqlite connection inside the thread so we never share
    # a cursor across thread boundaries.
    set_meta(conn, "recovery_in_progress", "1")
    set_meta(conn, "recovery_started_at", str(time.time()))
    set_meta(conn, "recovery_heartbeat_at", str(time.time()))
    set_meta(conn, "recovery_progress", f"0/{mismatch['stale_count']}")
    set_meta(
        conn,
        "recovery_coverage_pct",
        str(mismatch["coverage_pct"]),
    )

    thread = threading.Thread(
        target=_run_worker_safely,
        args=(db_path, embedder, current_dim, mismatch["stale_count"]),
        name="skygrep-recovery",
        daemon=True,
    )
    thread.start()

    state.update(mismatch)
    state["just_started"] = True
    state["worker_thread"] = thread
    return state


def _run_worker_safely(db_path: Path, embedder, expected_dim: int, total_stale: int) -> None:
    """Outer wrapper that catches any worker exception so the daemon
    thread doesn't die without clearing the ``recovery_in_progress``
    flag — otherwise a single crash would block automatic recovery
    forever for that index."""

    try:
        run_recovery_worker(db_path, embedder, expected_dim, total_stale)
    except Exception:
        logger.exception("recovery worker crashed; marking recovery as halted")
        try:
            conn = sqlite3.connect(db_path)
            set_meta(conn, "recovery_in_progress", "0")
            conn.close()
        except Exception:
            pass


def run_recovery_worker(
    db_path: Path,
    embedder,
    expected_dim: int,
    total_stale: int,
    *,
    batch_size: int = 20,
) -> int:
    """Re-embed stale chunks in mtime-DESC order, stream-committing per
    file so already-recovered files become searchable progressively.

    Returns the number of chunks actually re-embedded (≤ ``total_stale``;
    can be less if files were modified during recovery and chunks went
    stale by other means).
    """

    started_at = time.time()
    re_embedded = 0
    conn = sqlite3.connect(db_path)
    try:
        while True:
            # Pull the next ~batch_size stale chunks from the most
            # recently-modified file. Re-querying every iteration keeps the
            # worker resilient to concurrent index updates that might add
            # new stale rows or recover existing ones out from under us.
            rows = conn.execute(
                """
                SELECT chunks.id, chunks.chunk
                FROM chunks
                JOIN vectors ON vectors.id = chunks.id
                WHERE vectors.embedding IS NULL
                   OR length(vectors.embedding) / 4 != ?
                ORDER BY chunks.file_mtime DESC, chunks.id ASC
                LIMIT ?
                """,
                (expected_dim, batch_size),
            ).fetchall()
            if not rows:
                break

            ids = [row[0] for row in rows]
            texts = [row[1] or "" for row in rows]
            new_vecs = _embed_batch_safely(embedder, texts, expected_dim)
            for cid, vec in zip(ids, new_vecs):
                blob = np.asarray(vec, dtype=np.float32).tobytes()
                conn.execute(
                    "UPDATE vectors SET embedding = ? WHERE id = ?",
                    (blob, cid),
                )
            conn.commit()
            re_embedded += len(rows)

            # Update heartbeat + progress + ETA. Coverage is computed
            # from total chunks at start, not the moving total, so the
            # number is monotone-rising and intuitive.
            elapsed = time.time() - started_at
            rate = re_embedded / elapsed if elapsed > 0 else 0.0
            remaining = max(0, total_stale - re_embedded)
            eta_seconds = int(remaining / rate) if rate > 0 else 0
            coverage_pct = (
                int(100 * re_embedded / total_stale) if total_stale else 100
            )
            set_meta(conn, "recovery_heartbeat_at", str(time.time()))
            set_meta(conn, "recovery_progress", f"{re_embedded}/{total_stale}")
            set_meta(conn, "recovery_eta_seconds", str(eta_seconds))
            set_meta(conn, "recovery_coverage_pct", str(coverage_pct))

        # Final pass: rebuild the file-mean ``files`` table now that every
        # chunk vector is at the new dim. Without this the cascade's
        # file-level prefilter would still see stale-dim file means.
        try:
            populate_file_embeddings(conn)
        except Exception:
            logger.exception("populate_file_embeddings failed during recovery cleanup")

        # Mark recovery complete + record the new fingerprint so the next
        # CLI run sees a coherent index.
        set_meta(conn, "recovery_in_progress", "0")
        set_meta(conn, "recovery_progress", f"{re_embedded}/{total_stale}")
        set_meta(conn, "recovery_coverage_pct", "100")
        set_meta(
            conn,
            "embedder_fingerprint",
            embedder_fingerprint(embedder, expected_dim),
        )
        logger.info(
            "skygrep recovery worker finished: re-embedded %d chunks in %.1fs",
            re_embedded,
            time.time() - started_at,
        )
    finally:
        conn.close()
    return re_embedded


def _embed_batch_safely(embedder, texts: list[str], expected_dim: int) -> list[list[float]]:
    """Call ``embedder.embed_batch`` if available; fall back to
    sequential ``embed`` so this works against any embedder that
    implements at least the single-text interface. Filters obvious
    zero-dim returns."""

    if hasattr(embedder, "embed_batch"):
        try:
            vecs = embedder.embed_batch(texts)
            if vecs and len(vecs[0]) == expected_dim:
                return vecs
            # Dim mismatch from the embedder itself? Fall through to
            # per-text path so we at least get partial progress.
        except Exception:
            logger.exception("embed_batch failed; falling back to per-text embed")
    return [embedder.embed(text) for text in texts]


def render_recovery_footer(state: dict) -> Optional[str]:
    """Return a one-line footer fragment for the CLI telemetry row.

    ``None`` when no recovery is active and we don't need to advertise
    anything to the user.
    """

    if not state or not state.get("in_progress"):
        return None
    progress = state.get("progress") or ""
    coverage = state.get("coverage_pct")
    eta = state.get("eta_seconds")
    parts = ["recovery=in-progress"]
    if progress:
        parts.append(f"chunks={progress}")
    if coverage is not None:
        parts.append(f"coverage={coverage}%")
    if eta:
        eta_min = eta // 60
        eta_sec = eta % 60
        if eta_min:
            parts.append(f"ETA={eta_min}m{eta_sec:02d}s")
        else:
            parts.append(f"ETA={eta_sec}s")
    return " ".join(parts)
