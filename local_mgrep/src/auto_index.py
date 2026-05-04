"""Just-in-time project indexing.

The bare-form ``mgrep "<query>"`` UX needs the user to never type ``mgrep
index`` for a normal workflow. This module owns:

  - First-time index for a fresh project (DB doesn't exist or is empty).
  - Lightweight mtime-based incremental refresh on every search.
  - A throttle so consecutive queries don't pay the mtime scan repeatedly.
  - **Background-spawn indexing** so the first query in a fresh project
    completes in ~100 ms via a ripgrep fallback while the semantic index
    builds in another process.

The throttle state lives in a small ``meta`` table inside the project DB
itself. The background-spawn lockfile lives next to the DB at
``<db_path>.lock`` so a crashed indexer can be detected and re-spawned on
the next query.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import click

from . import config, storage
from .embeddings import get_embedder
from .hybrid import extract_query_terms, lexical_candidate_paths
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


# --------------------------------------------------------------------------- #
# Background-index lockfile + readiness primitives.                            #
# --------------------------------------------------------------------------- #


def _lock_path(db_path: Path) -> Path:
    return db_path.with_suffix(db_path.suffix + ".lock")


def is_index_ready(conn: sqlite3.Connection) -> bool:
    """An index is 'ready' iff a full pass has completed.

    Marker: the ``meta.last_full_index_at`` row is set by ``first_time_index``
    only after ``populate_file_embeddings`` succeeds. A partial chunks-but-
    no-files state (which happens when the indexer is killed mid-flight)
    therefore correctly reports 'not ready' — users get the rg fallback
    until the next successful index pass.
    """
    return _meta_get(conn, "last_full_index_at") is not None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM  # alive but not ours; treat as alive
    return True


def is_index_building(db_path: Path) -> bool:
    """Is a background indexer currently working on this DB?

    True iff ``<db>.lock`` exists with a PID that responds to signal 0.
    Stale lockfiles (process gone) are removed and report False.
    """
    lock = _lock_path(db_path)
    if not lock.exists():
        return False
    try:
        pid = int(lock.read_text().strip())
    except (OSError, ValueError):
        try:
            lock.unlink()
        except FileNotFoundError:
            pass
        return False
    if _pid_alive(pid):
        return True
    try:
        lock.unlink()
    except FileNotFoundError:
        pass
    return False


def spawn_background_index(root: Path, db_path: Path, *, force: bool = False) -> int | None:
    """Launch ``mgrep index <root>`` as a detached background process.

    Returns the spawned PID, or ``None`` if the indexer is already running
    (and ``force`` was False). The child writes its PID to
    ``<db_path>.lock`` and removes it on exit; the wrapper script mirrors
    this with ``flock`` semantics so a crashed child still cleans up.
    """
    if not force and is_index_building(db_path):
        return None
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_path(db_path)
    log = db_path.with_suffix(db_path.suffix + ".log")
    env = dict(os.environ)
    env["MGREP_DB_PATH"] = str(db_path)
    # Detach so the child survives our exit and isn't killed by the parent's
    # signal group. ``start_new_session`` matters on POSIX; it makes the
    # child the leader of a new session so SIGINT/SIGHUP from the parent
    # terminal don't propagate.
    proc = subprocess.Popen(
        [sys.executable, "-m", "local_mgrep.src.cli", "index", str(root)],
        cwd=str(root),
        env=env,
        stdout=open(log, "ab", buffering=0),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    try:
        lock.write_text(str(proc.pid))
    except OSError:
        # Best-effort: if we cannot write the lockfile the indexer still
        # runs but multiple invocations may race. Acceptable degradation.
        pass
    return proc.pid


# --------------------------------------------------------------------------- #
# Ripgrep-only fallback for the first query in a fresh project.                #
# --------------------------------------------------------------------------- #


def rg_fallback_results(
    query: str,
    root: Path,
    *,
    top_k: int,
    snippet_lines: int = 24,
) -> list[dict]:
    """Return result dicts shaped like ``storage.search`` output, sourced
    purely from ripgrep — no embedding model, no DB.

    Score = number of distinct query terms whose token hits in the file.
    Snippet = the first ``snippet_lines`` lines containing any query term,
    or just the file head when no line matches.
    """
    rg = shutil.which("rg")
    if not rg:
        return []
    terms = extract_query_terms(query)
    if not terms:
        return []
    file_hits: dict[str, set[str]] = {}
    for term in terms:
        try:
            r = subprocess.run(
                [rg, "-il", "-F", term, str(root)],
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            file_hits.setdefault(line, set()).add(term)
    if not file_hits:
        return []
    ranked = sorted(file_hits.items(), key=lambda kv: -len(kv[1]))[: top_k * 2]

    results: list[dict] = []
    term_pat = re.compile(
        "|".join(re.escape(t) for t in terms), flags=re.IGNORECASE
    ) if terms else None
    for path, hits in ranked:
        snippet, sl, el, lang = _read_snippet(Path(path), term_pat, snippet_lines)
        if snippet is None:
            continue
        results.append(
            {
                "path": path,
                "file": path,
                "chunk": snippet,
                "snippet": snippet,
                "language": lang,
                "start_line": sl,
                "end_line": el,
                "start_byte": None,
                "end_byte": None,
                "score": float(len(hits)) / max(1, len(terms)),
                "semantic_score": 0.0,
                "lexical_score": float(len(hits)) / max(1, len(terms)),
                "fallback": "ripgrep",
            }
        )
        if len(results) >= top_k:
            break
    return results


def _read_snippet(
    path: Path,
    term_pat: re.Pattern | None,
    snippet_lines: int,
) -> tuple[str | None, int | None, int | None, str | None]:
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None, None, None, None
    lines = text.splitlines()
    start = 0
    if term_pat is not None:
        for i, ln in enumerate(lines):
            if term_pat.search(ln):
                start = max(0, i - 3)
                break
    end = min(len(lines), start + snippet_lines)
    snippet = "\n".join(lines[start:end])
    suffix = path.suffix.lower().lstrip(".")
    return snippet, start + 1, end, suffix or None


def _refresh_throttle_from_env() -> float:
    raw = os.environ.get("MGREP_AUTO_REFRESH_THROTTLE_SECONDS")
    if not raw:
        return DEFAULT_REFRESH_THROTTLE_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_REFRESH_THROTTLE_SECONDS


# Lexical shortcut tuning. All four conditions must be satisfied for the
# shortcut to fire. Tuned conservatively so we never short-circuit a
# genuine semantic query — accuracy is the gold standard, speed is bonus.
_LEXICAL_MAX_QUERY_TERMS = 6
_LEXICAL_MAX_FILES = 10
_LEXICAL_MIN_PATH_TOKEN_OVERLAP = 2
_LEXICAL_MAX_PARENT_DIRS = 2


def lexical_shortcut(
    query: str,
    project_root: Path,
    *,
    top_k: int,
    max_query_terms: int = _LEXICAL_MAX_QUERY_TERMS,
    max_files: int = _LEXICAL_MAX_FILES,
    min_path_token_overlap: int = _LEXICAL_MIN_PATH_TOKEN_OVERLAP,
    max_parent_dirs: int = _LEXICAL_MAX_PARENT_DIRS,
) -> list[dict] | None:
    """Try to short-circuit cascade retrieval via ripgrep when the query is
    lexically friendly. Returns a results list shaped like
    ``rg_fallback_results`` if all four conservative conditions hold; else
    ``None`` so the caller falls through to semantic cascade.

    Conditions (ALL required):
      1. Query has <= ``max_query_terms`` non-stop-word tokens.
      2. ``rg`` returns >= 1 and <= ``max_files`` candidate files.
      3. At least one candidate's path contains
         >= ``min_path_token_overlap`` query tokens.
      4. Candidate files cluster in <= ``max_parent_dirs`` distinct
         parent directories.

    Conservative on every dimension: any borderline query falls through
    to cascade so semantic recall is never sacrificed for routing speed.
    """
    terms = extract_query_terms(query)
    # Condition 1: query is short enough to be plausibly lexical
    if not terms or len(terms) > max_query_terms:
        return None

    results = rg_fallback_results(query, project_root, top_k=top_k)
    if not results:
        return None
    paths = [r["path"] for r in results]

    # Condition 2: result set is small
    if len(paths) > max_files:
        return None

    # Condition 3: at least one path encodes >= min_path_token_overlap
    # query tokens — strong sign the user's vocabulary already aligns
    # with the code path vocabulary.
    lower_terms = [t.lower() for t in terms]
    max_overlap = 0
    for p in paths:
        p_lower = p.lower()
        overlap = sum(1 for t in lower_terms if t in p_lower)
        if overlap > max_overlap:
            max_overlap = overlap
    if max_overlap < min_path_token_overlap:
        return None

    # Condition 4: matches cluster in a small number of parent dirs
    parent_dirs = {str(Path(p).parent) for p in paths}
    if len(parent_dirs) > max_parent_dirs:
        return None

    # All conditions satisfied — annotate source and return.
    for r in results:
        r["fallback"] = "rg-shortcut"
    return results


# ----- v0.13.0 filename-lookup shortcut -----------------------------
#
# Some queries are filename lookups, not content searches —
# "where is eb1b file?", "find package.json", "show me the README".
# Neither the cascade (semantic content) nor the rg pre-gate (lexical
# content) handles them well: PDF / docx / binary files aren't even
# indexed, so semantic search is hopeless; ripgrep matching content
# returns the wrong files because the query token may not appear
# inside any text. The right tool is `find -iname '*token*'`.
#
# We add a third routing tier that fires BEFORE the lexical content
# shortcut. Conservative four-condition gate (same philosophy as
# v0.12.0): only fires when the query clearly looks like a filename
# lookup AND find returns a small, well-defined match set.

_FN_LOOKUP_INTENT = (
    "find ", "where is", "where's", "locate ", "show me",
    "look for", "search for", "open ",
)
_FN_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{2,40}")
_FN_QUESTION_WORDS = frozenset({
    "is", "are", "the", "a", "an", "of", "for", "me", "my", "to",
    "in", "on", "at", "by", "from", "with", "where", "find",
    "locate", "show", "look", "search", "open", "list", "get",
    "file", "files", "folder", "directory", "dir",
    "any", "some", "all", "this", "that", "these", "those",
    "and", "or", "but", "not",
})


def filename_shortcut(
    query: str,
    project_root: Path,
    *,
    top_k: int,
    max_files: int = 30,
    max_depth: int = 6,
) -> list[dict] | None:
    """Try to short-circuit search by interpreting the query as a
    filename lookup. Returns a result list (shaped like
    ``rg_fallback_results`` plus a ``size_kb`` / ``mtime_str`` metadata
    line) if all four conservative conditions hold; ``None`` to fall
    through to the lexical content shortcut and then the cascade.

    Conditions (ALL required):
      1. Query lowercased contains an explicit lookup-intent phrase
         (``find / where is / locate / show me / open ...``) **or**
         the standalone word ``file`` / ``files``.
      2. After stripping stop-words, at least one ``name-like`` token
         remains (length 3-40, alphanumeric with optional ``._-``).
      3. ``find -iname '*<token>*'`` returns >= 1 and <= ``max_files``
         actual files (not dirs, not dotfiles).
      4. The longest matching path's basename literally contains the
         token (case-insensitive). Guards against fluke substring
         matches deep in the tree.
    """
    q_lower = query.lower()

    # Condition 1: explicit lookup intent
    has_intent = any(phrase in q_lower for phrase in _FN_LOOKUP_INTENT)
    if not has_intent:
        # Allow standalone "file" / "files" as a softer trigger
        if not re.search(r"\bfiles?\b", q_lower):
            return None

    # Condition 2: extract a usable name token
    raw_tokens = _FN_TOKEN_RE.findall(query)
    candidates = [
        t for t in raw_tokens if t.lower() not in _FN_QUESTION_WORDS
    ]
    if not candidates:
        return None
    # Pick longest token (most specific identifier)
    pattern = max(candidates, key=len)
    if len(pattern) < 3:
        return None

    # Condition 3: find returns a clean small set
    find_bin = shutil.which("find")
    if not find_bin:
        return None
    try:
        r = subprocess.run(
            [
                find_bin, str(project_root),
                "-maxdepth", str(max_depth),
                "-iname", f"*{pattern}*",
                "-not", "-path", "*/.*",
                "-not", "-path", "*/node_modules/*",
                "-not", "-path", "*/.venv/*",
                "-not", "-path", "*/__pycache__/*",
                "-type", "f",
            ],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    paths = [
        line.strip() for line in r.stdout.splitlines() if line.strip()
    ]
    if not paths or len(paths) > max_files:
        return None

    # Condition 4: at least one basename actually contains the token
    pat_lower = pattern.lower()
    if not any(pat_lower in Path(p).name.lower() for p in paths):
        return None

    # Rank: shorter path first (less nested = likely the canonical),
    # then most-recently-modified within the same depth.
    def _sort_key(p: str) -> tuple:
        st = Path(p).stat() if Path(p).exists() else None
        depth = p.count("/")
        mtime = -st.st_mtime if st else 0.0
        return (depth, mtime)

    paths.sort(key=_sort_key)
    paths = paths[:top_k]

    results: list[dict] = []
    for p in paths:
        path_obj = Path(p)
        try:
            st = path_obj.stat()
            size_kb = st.st_size / 1024.0
            mtime_str = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(st.st_mtime)
            )
        except OSError:
            size_kb = 0.0
            mtime_str = "?"
        suffix = path_obj.suffix.lstrip(".") or "file"
        snippet = (
            f"size: {size_kb:>7.1f} KB    "
            f"modified: {mtime_str}    "
            f"type: {suffix}"
        )
        results.append({
            "path": p,
            "file": p,
            "chunk": snippet,
            "snippet": snippet,
            "language": suffix,
            "start_line": None,
            "end_line": None,
            "start_byte": None,
            "end_byte": None,
            "score": 1.0,
            "semantic_score": 0.0,
            "lexical_score": 1.0,
            "fallback": "filename-lookup",
        })
    return results


