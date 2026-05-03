"""Just-in-time project indexing.

The bare-form ``mgrep "<query>"`` UX needs the user to never type ``mgrep
index`` for a normal workflow. This module owns:

  - First-time index for a fresh project (DB doesn't exist or is empty).
  - Lightweight mtime-based incremental refresh on every search.
  - A throttle so consecutive queries don't pay the mtime scan repeatedly.

The throttle state lives in a small ``meta`` table inside the project DB
itself — no external file, no global cache, no cross-project surprise.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

import click

from . import config, storage
from .embeddings import get_embedder
from .indexer import batch_embed, collect_indexable_files, prepare_file_chunks

logger = logging.getLogger(__name__)


# Window during which we skip the mtime scan even if some files might have
# changed. Tunable via ``MGREP_AUTO_REFRESH_THROTTLE_SECONDS`` (seconds).
DEFAULT_REFRESH_THROTTLE_SECONDS = 30.0


def _ensure_meta_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
    )


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    _ensure_meta_table(conn)
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    _ensure_meta_table(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def index_status(conn: sqlite3.Connection) -> dict:
    """Return a structured snapshot of the project index for the status line."""
    chunk_n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    file_n = conn.execute("SELECT COUNT(DISTINCT file) FROM chunks").fetchone()[0]
    last_full = _meta_get(conn, "last_full_index_at")
    last_refresh = _meta_get(conn, "last_refresh_at")
    return {
        "chunks": chunk_n,
        "files": file_n,
        "last_full_index_at": float(last_full) if last_full else 0.0,
        "last_refresh_at": float(last_refresh) if last_refresh else 0.0,
    }


def _human_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hr ago"
    return f"{int(seconds // 86400)} d ago"


def index_age_human(conn: sqlite3.Connection, now: float | None = None) -> str:
    now = now or time.time()
    refresh = _meta_get(conn, "last_refresh_at") or _meta_get(conn, "last_full_index_at")
    if not refresh:
        return "never"
    return _human_age(now - float(refresh))


def first_time_index(
    conn: sqlite3.Connection,
    root: Path,
    *,
    embedder=None,
    quiet: bool = False,
) -> tuple[int, int]:
    """Run a full index of ``root`` with progress output.

    Returns ``(file_count, chunk_count)``. The progress line is printed to
    stderr so it does not pollute ``--json`` stdout.
    """

    files = collect_indexable_files(root)
    n_files = len(files)
    if not files:
        return 0, 0
    if not quiet:
        click.echo(
            f"⏳ Indexing {n_files} files in {root} (one-time setup) …",
            err=True,
        )
    if embedder is None:
        embedder = get_embedder()

    total_chunks = 0
    t0 = time.time()
    for i, f in enumerate(files, start=1):
        chunks = prepare_file_chunks(f, root=root)
        if chunks:
            chunks = batch_embed(chunks, embedder, batch_size=10)
            for c in chunks:
                storage.delete_file_chunks(conn, c["file"])
            storage.store_chunks_batch(conn, chunks)
            total_chunks += len(chunks)
        if not quiet and (i % 25 == 0 or i == n_files):
            elapsed = time.time() - t0
            click.echo(
                f"  · {i}/{n_files} files · {total_chunks} chunks · {elapsed:.1f}s",
                err=True,
            )
    storage.populate_file_embeddings(conn)
    now = time.time()
    _meta_set(conn, "last_full_index_at", str(now))
    _meta_set(conn, "last_refresh_at", str(now))
    _meta_set(conn, "indexed_root", str(root))
    if not quiet:
        click.echo(
            f"✓ Indexed {total_chunks} chunks across {n_files} files in {time.time()-t0:.1f}s",
            err=True,
        )
    return n_files, total_chunks


def incremental_refresh(
    conn: sqlite3.Connection,
    root: Path,
    *,
    throttle_seconds: float = DEFAULT_REFRESH_THROTTLE_SECONDS,
    quiet: bool = False,
) -> int:
    """Quick mtime-based incremental refresh.

    Returns the number of files that were re-embedded. Skips entirely when
    the previous refresh ran within ``throttle_seconds`` (no scan, no
    stat calls).
    """

    last = _meta_get(conn, "last_refresh_at")
    now = time.time()
    if last and now - float(last) < throttle_seconds:
        return 0

    indexed = storage.get_indexed_files(conn)
    files = collect_indexable_files(root)
    embedder = None
    refreshed = 0
    deleted_files = storage.delete_missing_files(conn, {str(f) for f in files}, root)
    for f in files:
        f_str = str(f)
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        prior = indexed.get(f_str)
        if prior is None or mtime > prior:
            if embedder is None:
                embedder = get_embedder()
            chunks = prepare_file_chunks(f, root=root)
            if chunks:
                chunks = batch_embed(chunks, embedder, batch_size=10)
                storage.delete_file_chunks(conn, f_str)
                storage.store_chunks_batch(conn, chunks)
                refreshed += 1
    if refreshed or deleted_files:
        storage.populate_file_embeddings(conn)
    _meta_set(conn, "last_refresh_at", str(now))
    if (refreshed or deleted_files) and not quiet:
        click.echo(
            f"↻ refreshed {refreshed} file(s)"
            + (f", removed {len(deleted_files)} stale" if deleted_files else "")
            + ".",
            err=True,
        )
    return refreshed


def ensure_indexed(
    db_path: Path,
    root: Path,
    *,
    auto_refresh: bool = True,
    quiet: bool = False,
    throttle_seconds: float | None = None,
) -> sqlite3.Connection:
    """Open (or create) the project DB; index on first use; optionally refresh.

    Returns an open connection. Caller closes it. Embedding model bootstrap
    is delegated to ``cli.search_cmd`` (we don't probe Ollama here so the
    no-change fast path doesn't pay any HTTP cost).
    """

    conn = storage.init_db(db_path)
    chunk_n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if chunk_n == 0:
        first_time_index(conn, root, quiet=quiet)
        return conn
    if auto_refresh:
        try:
            incremental_refresh(
                conn,
                root,
                throttle_seconds=throttle_seconds
                if throttle_seconds is not None
                else _refresh_throttle_from_env(),
                quiet=quiet,
            )
        except Exception as exc:
            # Refresh failures must not block search; the user can still
            # query the existing index.
            logger.warning("auto-refresh failed: %s", exc)
    return conn


def _refresh_throttle_from_env() -> float:
    raw = os.environ.get("MGREP_AUTO_REFRESH_THROTTLE_SECONDS")
    if not raw:
        return DEFAULT_REFRESH_THROTTLE_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_REFRESH_THROTTLE_SECONDS


