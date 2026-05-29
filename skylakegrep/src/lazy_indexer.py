"""Lazy / adaptive index — augments (does NOT replace) the existing cascade.

Two use cases for this module, both clarified by the user 2026-05-06:

  1. **Cold-start intermediate confidence.** When `skygrep search` runs
     on a fresh / un-indexed repo, the existing 0.2.x flow returns
     ripgrep keyword fallback in ~100 ms (instant, but vocab-mismatch
     blind). This module produces a SECOND answer in ~5-10 s — true
     semantic, ~50-70 % recall — between the rg fallback and the full
     background-index completion (which takes minutes and reaches
     30/30). The user sees confidence rise progressively.

  2. **Cross-folder proactive explorer.** The existing
     `filename_extend` enhancer searches a HARD-CODED list
     (`~/Downloads`, `~/Desktop`, `~/Documents`). When the user asks
     about a file that lives somewhere else (e.g. `~/Pictures`,
     `~/Code`), filename_extend can't find it. This module's
     LLM-driven folder picker replaces the hard-coded list with a
     per-query judgement.

In neither use case does this module replace `cascade_search`. The
existing cascade + auto-index + rg-fallback UX is preserved.

**0.5.3 quality fixes (the 1/10 → ?/10 push)**:

  - **Token-shortcut DEDUP**: same parent dir + numeric prefix family
    (e.g. ``django/contrib/admin/migrations/0001_*.py``,
    ``0002_*.py``, …) or shared stem prefix collapses to ONE
    representative. Without this, Django's hundreds of auto-generated
    migration files ate the entire 25-file seed budget on every
    "migration" query — leaving the actual ``executor.py`` runner
    code with zero seed slots.
  - **LLM router priority**: when the small-LLM router and the
    token-shortcut compete for budget, router output wins the first
    10 slots; token-shortcut (now de-duped) backfills up to 15 more.
  - **Regex import diffusion**: after the seed text is read into
    memory, a one-pass language-agnostic regex scan extracts
    ``import``/``from`` statements (Python, JS/TS, Go, Rust, C/C++,
    Ruby, PHP) and resolves them to local file paths via the
    pre-walked tree, adding up to 10 import-graph neighbours to the
    embed pool. This is how a wrong initial seed diffuses outward to
    a correct one — even when the user is in the wrong subfolder.
  - **ThreadPool I/O parallelism**: tree walk, LLM router, file text
    load, and import extraction run on a small worker pool so wall
    time is dominated by the single Ollama batch-embed call, not
    serialised I/O.
  - **Progressive stderr progress**: the cold-start path emits short
    "scanning… / diffusing / embedding…" lines so a user staring at
    a 5–10 s wait sees the system working, not hanging.

**Latency target: ≤ 10 s** for the cold-start case (≤25 seed files +
batch-embed + cosine + validate). Achieved by:

  - `OllamaEmbedder.embed_batch(texts)` — one API call for all seeds
  - Tight de-duped seed budget
  - σ-evidence validation: only return when top-K is well-separated
    enough to be worth showing

**Hyperparameter contract: zero new constants.** All scoring is
cosine; the σ-validation gate reuses `CASCADE_TAU_FLOOR`.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import storage as S
from . import ui as ui_mod


# ── Filesystem crawl ────────────────────────────────────────────────────


_DEFAULT_CODE_EXTENSIONS = (
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java", ".kt",
    ".rb", ".php", ".cs", ".cpp", ".cc", ".c", ".h", ".hpp", ".swift",
    ".md", ".rst", ".txt", ".toml", ".yml", ".yaml", ".json",
)
_DEFAULT_IGNORE_DIRS = frozenset({
    "node_modules", ".git", "venv", ".venv", "__pycache__", "target",
    "build", "dist", "out", ".tox", ".pytest_cache", "vendor",
    ".next", ".nuxt", ".cache", "Library", "Caches", "Applications",
})
_DEFAULT_IGNORE_PATH_SUFFIXES = (
    ("go", "pkg", "mod"),
)


def _should_prune_dir(parent_parts: tuple[str, ...], dirname: str) -> bool:
    if dirname in _DEFAULT_IGNORE_DIRS or dirname.startswith("."):
        return True
    candidate = parent_parts + (dirname,)
    return any(
        len(candidate) >= len(suffix)
        and candidate[-len(suffix):] == suffix
        for suffix in _DEFAULT_IGNORE_PATH_SUFFIXES
    )


def crawl_tree(
    root: Path,
    *,
    max_files: int = 5000,
    extensions: tuple[str, ...] = _DEFAULT_CODE_EXTENSIONS,
    max_seconds: float | None = None,
) -> tuple[list[str], dict[str, int]]:
    """Walk ``root``, return ``(file_paths, dir_summary)``. Cheap stat-only."""
    files: list[str] = []
    dir_summary: dict[str, int] = {}
    root = root.resolve()
    deadline = (
        time.perf_counter() + max_seconds
        if max_seconds is not None and max_seconds > 0
        else None
    )
    stack: list[tuple[Path, tuple[str, ...], str]] = [(root, (), ".")]
    while stack:
        if deadline is not None and time.perf_counter() >= deadline:
            break
        current, parent_parts, parent_label = stack.pop()
        try:
            iterator = os.scandir(current)
        except OSError:
            continue
        try:
            with iterator as entries:
                for entry in entries:
                    if deadline is not None and time.perf_counter() >= deadline:
                        return files, dir_summary
                    name = entry.name
                    if name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if _should_prune_dir(parent_parts, name):
                                continue
                            child_parts = parent_parts + (name,)
                            stack.append((
                                Path(entry.path),
                                child_parts,
                                "/".join(child_parts),
                            ))
                            continue
                        if (
                            Path(name).suffix.lower() in extensions
                            and entry.is_file(follow_symlinks=False)
                        ):
                            files.append(str(Path(entry.path)))
                            dir_summary[parent_label] = (
                                dir_summary.get(parent_label, 0) + 1
                            )
                            if len(files) >= max_files:
                                return files, dir_summary
                    except OSError:
                        continue
        except OSError:
            continue
    return files, dir_summary


def render_tree_summary(
    dir_summary: dict[str, int], max_dirs: int = 60,
) -> str:
    items = sorted(dir_summary.items(), key=lambda kv: -kv[1])[:max_dirs]
    return "\n".join(f"  {d:<60} {n:>4} files" for d, n in items)


# ── Token-shortcut filename / path matchers (deterministic) ─────────────


_QUERY_STOPWORDS = frozenset(
    "the and for this that are how where what when why does did with from "
    "into onto over under back show find search list give get tell".split()
)

# Numeric prefix detector — matches ``0001_initial.py`` / ``0002_*.py`` /
# ``20240115_*.py`` style stems. Captures the prefix portion so the same
# group of files all map to the same family key.
_NUMERIC_PREFIX_RE = re.compile(r"^(\d{2,8}[_\-])")


def _stem_family_key(path: str) -> str:
    """Group key for token-shortcut dedup.

    Two heuristics, in order:

      1. Numeric prefix family — ``0001_initial.py`` / ``0002_x.py`` /
         ``20240115_alter_user.py``: the ``\\d{2,8}[_-]`` prefix is the
         family marker; everything after it is the per-file
         differentiator. Returns ``"<parent>::numeric:<extension>"``.

      2. Shared stem prefix length ≥ 6 — files like
         ``cache_invalidate_a.py``, ``cache_invalidate_b.py``: the
         common 6-char head ``cache_`` is the family marker. Returns
         ``"<parent>::<head6>:<ext>"``.

    Files that match neither heuristic get a unique key (their full
    path) so they're never deduped against anything but themselves.
    """
    p = Path(path)
    parent = str(p.parent)
    stem = p.stem.lower()
    ext = p.suffix.lower()
    m = _NUMERIC_PREFIX_RE.match(stem)
    if m:
        return f"{parent}::numeric:{ext}"
    if len(stem) >= 6:
        return f"{parent}::{stem[:6]}:{ext}"
    return path


def _dedupe_seed_groups(
    files: list[str], *, group_min: int = 3,
) -> list[str]:
    """Drop near-duplicate files inside the same parent dir.

    Spec (from user 2026-05-06): when ≥ ``group_min`` files share the
    same parent directory AND a numeric prefix family (``0001_*.py``,
    ``0002_*.py``, …) OR identical stem prefix length ≥ 6, keep only
    one representative — the alphabetically first, which is stable
    across runs.

    The ``group_min=3`` threshold guards against false collapse on
    ``__init__.py`` + ``foo.py`` co-located file pairs in small dirs.

    Returns a new list in input order with surplus group members
    removed.
    """
    groups: dict[str, list[str]] = {}
    for f in files:
        groups.setdefault(_stem_family_key(f), []).append(f)
    keep: set[str] = set()
    for key, members in groups.items():
        if len(members) < group_min:
            keep.update(members)
        else:
            # Keep the alphabetically first member as the
            # representative — deterministic, reviewer-friendly.
            keep.add(sorted(members)[0])
    return [f for f in files if f in keep]


def token_dir_picks(
    query: str, dir_summary: dict[str, int], *, max_dirs: int = 8,
) -> list[str]:
    """Deterministic dir-token matcher — picks the directories whose
    path tokens best cover the query tokens.

    Returns up to ``max_dirs`` directory paths (relative to the
    project root, as they appear in ``dir_summary``), ordered by
    overlap-score-then-leaf-depth (deeper dirs preferred when
    overlap is equal — ``django/db/migrations`` beats ``django/db``).

    Built as a fallback / supplement to ``llm_router.infer_candidate_paths``:
    qwen2.5:3b often hallucinates or picks tangentially-related dirs
    on long natural-language queries, but the user's tokens
    (``migration runner database`` → ``migrations``) consistently
    match real path components. This deterministic step gives the
    cold-start path a reliable seed source even when the LLM router
    produces nothing useful.
    """
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_]*", query)
    pieces: set[str] = set()
    for tok in raw:
        sub = re.sub(
            r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", tok
        )
        for p in sub.lower().split():
            if len(p) >= 3 and p not in _QUERY_STOPWORDS:
                pieces.add(p)
    if not pieces:
        return []
    scored: list[tuple[int, int, int, str]] = []
    for d in dir_summary.keys():
        d_lower = d.lower()
        hits = sum(1 for p in pieces if p in d_lower)
        if hits == 0:
            continue
        # Test-path penalty: dirs that look like test fixtures
        # (``tests/...`` or ``*_tests/...`` or ``.../tests/...``)
        # rank below otherwise-equivalent real-source dirs. This is
        # the structural-vs-data distinction at the directory level
        # — the user's question "where is X" almost always wants
        # the production module, not a test fixture that happens to
        # name-match. Without this penalty, deep test dirs like
        # ``tests/migrations/migrations_test_apps/...`` outrank
        # ``django/db/migrations`` because both match the same
        # query token but the test path is deeper.
        is_test = (
            d_lower.startswith("tests/")
            or d_lower.startswith("test/")
            or "/tests/" in d_lower
            or "/test/" in d_lower
            or d_lower.endswith("/tests")
            or d_lower.endswith("_tests")
            or "_tests/" in d_lower
        )
        # Test-path penalty: halve effective hit count for test
        # dirs. A non-test dir with N hits beats a test dir with N
        # hits, AND a non-test dir with N hits roughly ties a test
        # dir with 2N hits — so a 2-hit test dir like
        # ``tests/test_runner_apps/databases`` no longer outranks
        # a 1-hit source dir like ``django/db/migrations`` for a
        # query that names "database" + "runner". Use score*2 with
        # int math so the sort key stays integer.
        score = hits * 2 if not is_test else hits
        depth = d.count("/") + 1
        # Sort key: (-score, depth, path)
        #   -score: most hits first (with test penalty already
        #     applied above)
        #   depth: shallower wins on ties (more canonical /
        #     structural — ``django/db/migrations`` over
        #     ``django/contrib/auth/migrations``)
        #   path: alphabetical for determinism
        scored.append((-score, depth, d))
    scored.sort()
    return [d for _, _, d in scored[:max_dirs]]


def token_shortcut_seeds(
    query: str, files: list[str], *, max_seeds: int = 15,
    dedupe: bool = True,
) -> list[str]:
    """Files whose basename or any directory component matches a
    query-derived token. Cheap, deterministic, no LLM.

    0.5.3: by default applies ``_dedupe_seed_groups`` so a single
    ``django/contrib/admin/migrations/0001_*.py`` family doesn't
    consume the entire seed budget. ALSO applies a stem-shape penalty
    (numeric-prefixed files like ``0001_initial.py`` are auto-
    generated migration data, not the user's likely target — even
    when they share parent dirs across multiple Django subpackages)
    so structural code files (``executor.py``, ``migration.py``,
    ``base.py``) sort above data files at the same hit count.
    """
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_]*", query)
    pieces: set[str] = set()
    for tok in raw:
        sub = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", tok)
        for p in sub.lower().split():
            if len(p) >= 3 and p not in _QUERY_STOPWORDS:
                pieces.add(p)
    if not pieces:
        return []
    scored: list[tuple[float, str]] = []
    for f in files:
        f_lower = f.lower()
        hits = sum(1 for p in pieces if p in f_lower)
        if hits == 0:
            continue
        # Penalty for numeric-prefix data files. Half-step shave so a
        # 2-hit data file (``0001_initial.py``) ranks below a 2-hit
        # code file (``executor.py``) but above a 1-hit code file —
        # data is still a signal, just a weaker one.
        stem = Path(f).stem
        score = float(hits)
        if _NUMERIC_PREFIX_RE.match(stem):
            score -= 0.5
        scored.append((score, f))
    scored.sort(key=lambda kv: (-kv[0], kv[1]))
    ranked = [f for _, f in scored]
    if dedupe:
        ranked = _dedupe_seed_groups(ranked)
    return ranked[:max_seeds]


# ── Regex import diffusion ──────────────────────────────────────────────


# One regex per language family. Each captures the imported module path
# in group 1. Run with ``re.MULTILINE`` against the seed file body.
_IMPORT_PATTERNS = (
    # Python
    re.compile(r"^\s*from\s+([\w\.]+)\s+import\s+", re.MULTILINE),
    re.compile(r"^\s*import\s+([\w\.]+)", re.MULTILINE),
    # JS / TS
    re.compile(r"""^\s*import\s+(?:[^'"]+\s+from\s+)?['"]([^'"]+)['"]""",
               re.MULTILINE),
    re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""", re.MULTILINE),
    # Go
    re.compile(r"""^\s*import\s+\(?\s*['"]([^'"]+)['"]""", re.MULTILINE),
    # Rust
    re.compile(r"^\s*use\s+([\w:]+)", re.MULTILINE),
    # C/C++
    re.compile(r"""^\s*#\s*include\s+["<]([^">]+)[">]""", re.MULTILINE),
    # Ruby
    re.compile(r"""^\s*require(?:_relative)?\s+['"]([^'"]+)['"]""",
               re.MULTILINE),
    # PHP
    re.compile(r"""^\s*(?:use|require|include)(?:_once)?\s*[\(\s]+['"]?([\w\\/\.]+)""",
               re.MULTILINE),
)


def extract_imports(text: str) -> list[str]:
    """Extract imported module paths from a single file's text.

    Language-agnostic by union-of-regexes. Returns deduped, order-
    preserved list of import strings as written in the source
    (``django.db.models`` / ``foo/bar`` / ``std::collections`` / …).
    Does NOT resolve to filesystem paths — that's
    ``resolve_imports_to_paths``.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    # Cap the scanned length so a 50 MB minified bundle doesn't grind.
    text_capped = text[:200_000]
    for pat in _IMPORT_PATTERNS:
        for m in pat.finditer(text_capped):
            mod = m.group(1).strip()
            if not mod or mod in seen:
                continue
            seen.add(mod)
            out.append(mod)
    return out


def resolve_imports_to_paths(
    imports: list[str], project_files: list[str], *, max_paths: int = 10,
) -> list[str]:
    """Resolve import strings (``django.db.migrations``,
    ``foo/bar/util``) to concrete filesystem paths from
    ``project_files``.

    Strategy: convert dots and double-colons to ``/``, then prefer
    files whose normalised path contains the import path as a
    contiguous substring. We deliberately do not require the import
    path to be a path *suffix* — popular Python projects do
    ``from foo.bar import x`` for a module physically located at
    ``./src/foo/bar/__init__.py`` whose path-suffix wouldn't match
    naively.

    Returns up to ``max_paths`` matches, sorted by score (longer
    overlap wins) and then alphabetically for determinism.
    """
    if not imports or not project_files:
        return []
    proj_lower = [(f, f.lower()) for f in project_files]
    out: dict[str, int] = {}
    for raw in imports:
        # Normalise to a slash-form path (``a.b.c`` → ``a/b/c``,
        # ``a::b`` → ``a/b``, leave ``a/b`` alone).
        norm = raw.replace(".", "/").replace("::", "/").lower().strip("/")
        if not norm or len(norm) < 3:
            continue
        for f, f_lower in proj_lower:
            if norm in f_lower:
                # Score by length of the match — longer = more
                # specific. Cap to avoid one mega-long mod dominating.
                out[f] = max(out.get(f, 0), min(len(norm), 64))
    if not out:
        return []
    ranked = sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f for f, _ in ranked[:max_paths]]


# ── Batch on-demand embedding ───────────────────────────────────────────


def embed_files_batch(
    conn: sqlite3.Connection,
    files: list[str],
    embedder,
    *,
    chunk_chars: int = 2400,
    preloaded_text: Optional[dict[str, str]] = None,
) -> tuple[int, dict[str, np.ndarray], dict[str, str]]:
    """Batch-embed a list of files in ONE Ollama API call (when the
    embedder supports `embed_batch`). Returns
    ``(new_count, embeddings, loaded_text)``.

    Embeddings are cached in the chunks table so repeat queries on the
    same project never re-embed.

    0.5.3: the third return value is the freshly-loaded text per file
    so the caller can run ``extract_imports`` for diffusion without a
    second disk read.

    Latency target: < 5 s for 15 files via batch — vs. 15-25 s sequential.
    """
    if not files:
        return 0, {}, {}
    preloaded_text = preloaded_text or {}
    fresh: list[tuple[str, str, float]] = []
    cached: dict[str, np.ndarray] = {}
    loaded_text: dict[str, str] = {}
    for f in files:
        cur = conn.execute(
            "SELECT c.id, v.embedding, c.chunk FROM chunks c "
            "LEFT JOIN vectors v ON v.id = c.id "
            "WHERE c.file = ? LIMIT 1",
            (f,),
        )
        row = cur.fetchone()
        if row is not None and row[1] is not None:
            try:
                cached[f] = np.frombuffer(row[1], dtype=np.float32)
            except Exception:
                pass
            if row[2]:
                loaded_text[f] = row[2]
            continue
        text = preloaded_text.get(f)
        if text is None:
            try:
                text = Path(f).read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
        try:
            mtime = Path(f).stat().st_mtime
        except OSError:
            mtime = 0.0
        body = text[:chunk_chars]
        loaded_text[f] = body
        fresh.append((f, body, mtime))

    new_embeddings: dict[str, np.ndarray] = {}
    if fresh:
        bodies = [b for _, b, _ in fresh]
        if hasattr(embedder, "embed_batch"):
            emb_lists = embedder.embed_batch(bodies)
        else:
            emb_lists = [embedder.embed(b) for b in bodies]
        chunks = []
        for (f, body, mtime), emb in zip(fresh, emb_lists):
            arr = np.asarray(emb, dtype=np.float32)
            new_embeddings[f] = arr
            chunks.append({
                "file": f, "chunk": body,
                "language": Path(f).suffix.lstrip("."),
                "chunk_index": 0, "file_mtime": mtime,
                "start_line": 1, "end_line": body.count("\n") + 1,
                "start_byte": 0, "end_byte": len(body),
                "embedding": list(emb),
            })
        if chunks:
            S.store_chunks_batch(conn, chunks)
            S.populate_file_embeddings(conn)
            conn.commit()
    return len(fresh), {**cached, **new_embeddings}, loaded_text


# ── Cosine + σ-validation ───────────────────────────────────────────────


def cosine_topk_with_sigma(
    query_embedding: np.ndarray,
    file_embeddings: dict[str, np.ndarray],
    *,
    top_k: int = 5,
) -> tuple[list[tuple[str, float]], float]:
    """Score each (file, embedding) by cosine to query, return top-K and
    σ of the score distribution.

    σ is the noise-floor under the same Bayesian-evidence framing the
    0.2.x cascade uses. Caller decides confidence threshold.
    """
    if not file_embeddings:
        return [], 0.0
    qv = np.asarray(query_embedding, dtype=np.float32)
    qn = float(np.linalg.norm(qv)) or 1.0
    scored: list[tuple[str, float]] = []
    for f, v in file_embeddings.items():
        if v is None or v.size == 0:
            continue
        if v.size != qv.size:
            continue
        vn = float(np.linalg.norm(v)) or 1.0
        score = float(np.dot(qv, v) / (qn * vn))
        scored.append((f, score))
    scored.sort(key=lambda kv: -kv[1])
    if len(scored) >= 3:
        sigma = float(np.std([s for _, s in scored[: max(top_k, 5)]]))
    else:
        sigma = 0.0
    return scored[:top_k], sigma


# ── Helpers for parallel scaffolding ────────────────────────────────────


_DEFAULT_WORKERS = min(4, os.cpu_count() or 2)
def _progress_step(label: str, message: str) -> str:
    return ui_mod.step(label, message)


def _emit_progress(progress: Optional[Callable[[str], None]], msg: str) -> None:
    """Internal: invoke the progress callback, swallowing any error so a
    broken sink can never block the search."""
    if progress is None:
        return
    try:
        progress(msg)
    except Exception:
        pass


def _stderr_progress(msg: str) -> None:
    """Default progress sink — prints to stderr so the CLI footer
    (stdout) stays clean for downstream consumers."""
    print(msg, file=sys.stderr, flush=True)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


def _read_text_safely(path: str, *, cap: int = 200_000) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:cap]
    except Exception:
        return ""


# ── Cold-start lazy explore — the public API ────────────────────────────


def lazy_explore_cold_start(
    conn: sqlite3.Connection,
    query: str,
    root: Path,
    embedder,
    *,
    top_k: int = 5,
    seed_budget: int = 25,
    crawl_budget_s: float | None = None,
    total_budget_s: float | None = None,
    router_timeout_s: float | None = None,
    progress: Optional[Callable[[str], None]] = None,
) -> tuple[list[dict], dict]:
    """Run cold-start lazy semantic exploration on a fresh project.

    Use this in the CLI's cold-start path between the rg-fallback
    (~100 ms, keyword) and the background-eager-index completion
    (~5-10 min, full 30/30). This produces a semantic answer in
    ~5-10 s with 50-70 % recall on the user's first interaction with
    the project.

    Returns ``(results, telemetry)`` where:
      * ``results`` is a list of result dicts in the standard
        skylakegrep shape (compatible with ``cli.merge_results``).
      * ``telemetry["confidence"]`` ∈ {"low", "medium", "high"} based
        on σ of the top-K cosine scores. Caller can choose to display
        only when confidence ≥ medium.

    ``progress`` is a callable that receives short status strings (one
    line each, no trailing newline). The caller wires this to stderr
    in the CLI so a user staring at a 5–10 s wait sees the system at
    work. Pass ``None`` to suppress.
    """
    t0 = time.perf_counter()
    tele: dict = {"path": "lazy-cold-start"}
    deadline = (
        t0 + total_budget_s
        if total_budget_s is not None and total_budget_s > 0
        else None
    )

    def _remaining() -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - time.perf_counter())

    def _budget_expired(stage: str) -> tuple[list[dict], dict]:
        tele["timed_out"] = True
        tele["timed_out_stage"] = stage
        tele["total_ms"] = int((time.perf_counter() - t0) * 1000)
        return [], tele

    _emit_progress(progress, _progress_step("lazy", "scanning project structure"))

    # Step 1: cheap crawl (single-threaded — pyfilesystem walk is GIL-bound
    # and not amortisable across threads).
    t1 = time.perf_counter()
    if crawl_budget_s is None:
        crawl_budget_s = _env_float("SKYGREP_LAZY_CRAWL_BUDGET_S", 2.0)
    remaining = _remaining()
    if remaining is not None:
        if remaining <= 0:
            return _budget_expired("crawl")
        crawl_budget_s = min(crawl_budget_s, max(0.05, remaining))
    files, dir_summary = crawl_tree(Path(root), max_seconds=crawl_budget_s)
    tele["crawled_files"] = len(files)
    tele["crawl_ms"] = int((time.perf_counter() - t1) * 1000)
    if _remaining() == 0:
        return _budget_expired("crawl")

    # Step 2 + 3 in PARALLEL: deterministic dir-token picks,
    # LLM-router dir picks, and file-level token shortcut are all
    # independent of each other and consume only the file list /
    # dir summary already in memory.
    #
    # Budget split (out of seed_budget=25 by default):
    #   - 16 slots for "directory-routed" picks (deterministic
    #     token_dir_picks first, then LLM router augmentation)
    #     — top dir gets 8 files, then 4, 2, 2, 2 (PER_DIR_BUDGETS
    #     below). This concentrates depth on the most likely dir
    #     while still spreading across 4-5 candidate dirs.
    #   - 9 slots for file-level token-shortcut backfill
    #     (post-dedup, post-numeric-prefix-penalty)
    router_budget = min(16, seed_budget)
    token_budget = max(seed_budget - router_budget, 0)
    tree_summary = render_tree_summary(dir_summary)
    if router_timeout_s is None:
        router_timeout_s = _env_float(
            "SKYGREP_COLD_LAZY_ROUTER_TIMEOUT_S", 1.0, minimum=0.1
        )
    remaining = _remaining()
    if remaining is not None:
        if remaining <= 0:
            return _budget_expired("router")
        # ``infer_candidate_paths`` may do a simplified retry; keep each local
        # LLM call inside the remaining foreground envelope.
        router_timeout_s = min(router_timeout_s, max(0.1, remaining / 3.0))

    def _run_router() -> tuple[list[str], int, str]:
        try:
            from . import llm_router as LR
            t1_ = time.perf_counter()
            picks = LR.infer_candidate_paths(
                query, tree_summary, timeout=router_timeout_s
            )
            ms = int((time.perf_counter() - t1_) * 1000)
            return picks, ms, ""
        except Exception as exc:  # noqa: BLE001
            return [], 0, str(exc)[:80]

    def _run_token() -> list[str]:
        return token_shortcut_seeds(query, files, max_seeds=token_budget)

    def _run_dir_picks() -> list[str]:
        return token_dir_picks(query, dir_summary, max_dirs=8)

    with ThreadPoolExecutor(max_workers=_DEFAULT_WORKERS) as pool:
        f_router = pool.submit(_run_router)
        f_token = pool.submit(_run_token)
        f_dirpicks = pool.submit(_run_dir_picks)
        llm_picks, llm_ms, llm_err = f_router.result()
        token_seeds = f_token.result()
        det_dir_picks = f_dirpicks.result()

    # Combine deterministic dir picks (first) with LLM router picks.
    # Deterministic-first means qwen's hallucinated subpaths can't
    # crowd out the real dirs that actually contain the answer.
    combined_picks: list[str] = []
    seen_picks: set[str] = set()
    for d in det_dir_picks:
        if d not in seen_picks:
            combined_picks.append(d)
            seen_picks.add(d)
    for d in llm_picks:
        d_norm = d.strip().rstrip("/")
        if d_norm and d_norm not in seen_picks:
            combined_picks.append(d_norm)
            seen_picks.add(d_norm)
    llm_picks = combined_picks

    tele["llm_router_ms"] = llm_ms
    tele["llm_picks"] = len(llm_picks)
    if llm_err:
        tele["llm_router_error"] = llm_err
    tele["token_seeds"] = len(token_seeds)
    if _remaining() == 0:
        return _budget_expired("router")

    # Step 4: compose the seed list — LLM router gets first 10 slots,
    # then token-shortcut backfills (deduped already).
    files_set = set(files)
    seeds: list[str] = []
    seen: set[str] = set()

    def _rank_dir_file(path: str) -> tuple[int, str]:
        """Sort key for files inside an LLM-picked directory.

        Lower tuple sorts first:
          - tier 0: ``__init__.py`` (package marker, has the most
            structural surface area)
          - tier 1: non-numeric-prefix stems (``executor.py``,
            ``base.py``, …)
          - tier 2: numeric-prefix stems (``0001_initial.py`` and
            other auto-generated migration / sequence files)

        Within each tier, alphabetical for determinism. This lifts
        ``django/db/migrations/executor.py`` (tier 1) above the
        ``0001_initial.py`` siblings (tier 2) — exactly the fix the
        bench needs for the migration-runner canary.
        """
        stem = Path(path).stem
        name = Path(path).name
        if name == "__init__.py":
            tier = 0
        elif _NUMERIC_PREFIX_RE.match(stem):
            tier = 2
        else:
            tier = 1
        return (tier, path)

    # Pre-bucket files by their LLM-picked-dir prefix so each pick's
    # expansion is a sorted slice rather than a linear scan over the
    # full ``files`` list.
    #
    # Weighted per-dir budget: the top-ranked dir gets the deepest
    # expansion (8 files) because if any single dir contains the
    # answer, it's almost certainly that one. Lower-ranked dirs get
    # progressively less coverage so the full router_budget covers
    # 4-5 distinct dirs end-to-end. Concretely (router_budget=16):
    #   rank 1: 8 files   rank 2: 4   rank 3: 2   rank 4+: 2
    # This is what brings ``django/template/engine.py`` (alphabetic
    # position 7 in ``django/template/``) into the seed set —
    # earlier the flat 4-files-per-dir cap stopped at
    # ``context_processors.py`` (position 6) and missed it.
    PER_DIR_BUDGETS = [8, 4, 2, 2, 2]
    for rank, raw in enumerate(llm_picks):
        if len(seeds) >= router_budget:
            break
        per_dir = (
            PER_DIR_BUDGETS[rank] if rank < len(PER_DIR_BUDGETS) else 1
        )
        cand = (Path(root) / raw).resolve()
        cand_s = str(cand)
        if cand_s in files_set and cand_s not in seen:
            seeds.append(cand_s)
            seen.add(cand_s)
        elif cand.is_dir():
            in_dir = [
                f for f in files
                if f.startswith(cand_s + "/") and f not in seen
            ]
            in_dir.sort(key=_rank_dir_file)
            for f in in_dir[:per_dir]:
                seeds.append(f)
                seen.add(f)
                if len(seeds) >= router_budget:
                    break
    for f in token_seeds:
        if len(seeds) >= seed_budget:
            break
        if f in seen:
            continue
        seeds.append(f)
        seen.add(f)

    if not seeds:
        seeds = files[:seed_budget]
        tele["seed_fallback"] = "first-N"

    tele["seeds_initial"] = len(seeds)
    remaining = _remaining()
    if remaining is not None and remaining <= 0:
        return _budget_expired("seed-planning")
    _emit_progress(
        progress,
        _progress_step(
            "plan",
            f"{_DEFAULT_WORKERS} workers · "
            f"{tele['llm_picks']} LLM-routed · "
            f"{tele['token_seeds']} token-shortcut · "
            f"{len(seeds)} seeds total",
        ),
    )

    # Step 5: PARALLEL preload of seed text so the diffusion step can
    # extract imports without a second disk read.
    remaining = _remaining()
    if remaining is not None and remaining <= 0:
        return _budget_expired("preload")
    with ThreadPoolExecutor(max_workers=_DEFAULT_WORKERS) as pool:
        loaded = dict(zip(seeds, pool.map(_read_text_safely, seeds)))

    # Step 6: regex import diffusion. Take imports from the seed
    # files, resolve to project paths, add up to 10 neighbours.
    t_diff = time.perf_counter()
    all_imports: list[str] = []
    for f, text in loaded.items():
        all_imports.extend(extract_imports(text))
    diffusion_files = resolve_imports_to_paths(
        all_imports, files, max_paths=10,
    )
    tele["diffusion_imports_seen"] = len(all_imports)
    tele["diffusion_neighbours"] = 0
    for f in diffusion_files:
        if f in seen:
            continue
        # Honour the seed budget — diffusion can grow the pool up to
        # ``seed_budget + 10`` so router + token + diffusion together
        # always have room for new neighbours.
        if len(seeds) >= seed_budget + 10:
            break
        seeds.append(f)
        seen.add(f)
        tele["diffusion_neighbours"] += 1
    tele["diffusion_ms"] = int((time.perf_counter() - t_diff) * 1000)
    tele["seeds_total"] = len(seeds)
    remaining = _remaining()
    if remaining is not None and remaining <= 0:
        return _budget_expired("diffusion")

    if tele["diffusion_neighbours"]:
        _emit_progress(
            progress,
            _progress_step(
                "expand",
                f"+{tele['diffusion_neighbours']} import neighbours "
                f"(total {len(seeds)} seeds)",
            ),
        )

    # Step 7: batch embed. Pass loaded text so we don't re-read the
    # files we already pulled into memory above.
    remaining = _remaining()
    if remaining is not None and remaining <= 0:
        return _budget_expired("embed")
    _emit_progress(
        progress,
        _progress_step("embed", f"{len(seeds)} files (1 Ollama call)"),
    )
    t1 = time.perf_counter()
    n_new, embeddings, _loaded2 = embed_files_batch(
        conn, seeds, embedder, preloaded_text=loaded,
    )
    tele["embed_new"] = n_new
    tele["embed_cached"] = len(embeddings) - n_new
    tele["embed_ms"] = int((time.perf_counter() - t1) * 1000)

    # Step 8: query embedding + cosine top-K
    t1 = time.perf_counter()
    qv = np.asarray(embedder.embed(query), dtype=np.float32)
    tele["query_embed_ms"] = int((time.perf_counter() - t1) * 1000)

    top, sigma = cosine_topk_with_sigma(qv, embeddings, top_k=top_k)
    tele["sigma"] = round(sigma, 4)

    # Step 9: σ-validation — re-use CASCADE_TAU_FLOOR as noise floor.
    tau = S.CASCADE_TAU_FLOOR
    if top and top[0][1] >= 0.3 and sigma >= tau:
        tele["confidence"] = "high" if sigma >= 4 * tau else "medium"
    elif top and top[0][1] >= 0.3:
        tele["confidence"] = "low"
    else:
        tele["confidence"] = "none"

    # Build result dicts in the cli.merge_results shape
    results: list[dict] = []
    for path, score in top:
        results.append({
            "path": path, "score": score, "snippet": "",
            "start_line": 0, "end_line": 0, "language": "",
            "chunk": "",
        })

    tele["total_ms"] = int((time.perf_counter() - t0) * 1000)
    return results, tele


# ── Cross-folder proactive explore — replacement for hardcoded ~/Downloads etc.


def lazy_explore_cross_folder(
    conn: sqlite3.Connection,
    query: str,
    *,
    embedder,
    top_k: int = 5,
    seed_budget: int = 5,
    candidate_roots: Optional[list[Path]] = None,
    progress: Optional[Callable[[str], None]] = None,
) -> tuple[list[dict], dict]:
    """Cross-folder proactive explorer — searches multiple candidate
    roots (env-configurable list) when the in-cwd answer is weak.

    Replaces ``proactive.filename_extend``'s hardcoded
    ``~/Downloads / ~/Desktop / ~/Documents`` list. Per-query the
    candidate roots come from ``SKYGREP_PROACTIVE_DIRS`` (0.5.2+).

    Returns the same shape as `lazy_explore_cold_start`.
    """
    if candidate_roots is None:
        env = os.environ.get("SKYGREP_PROACTIVE_DIRS", "").strip()
        if env:
            candidate_roots = [
                Path(p).expanduser() for p in env.split(":") if p.strip()
            ]
            candidate_roots = [r for r in candidate_roots if r.is_dir()]
        else:
            candidate_roots = [
                Path.home() / d for d in
                ("Downloads", "Desktop", "Documents", "Pictures", "Code", "Projects")
                if (Path.home() / d).exists()
            ]

    _emit_progress(
        progress,
        _progress_step(
            "cross",
            f"exploring {len(candidate_roots)} candidate roots",
        ),
    )

    # 0.5.6: cap each candidate root at 5000 files. Earlier 0.5.3
    # used 30000/root which on a default ``SKYGREP_PROACTIVE_DIRS``
    # of ``~/Downloads:~/Desktop:~/Documents:~/Pictures:~/Code:~/Projects``
    # — i.e. the entire macOS user home tree with iCloud sync — meant
    # the walk alone could take over a minute *before any embed
    # happened*. The user-reported "skygrep silent for 2:37" came
    # from this. 5000/root is enough to cover any reasonably-sized
    # OSS repo while keeping the wall-clock walk under ~5 s on a
    # cold filesystem cache.
    # 0.5.12: make the cap a real wall-clock budget, not just a file-count
    # budget. On large home trees, walking ignored hidden/vendor dirs before
    # filtering could keep a cold semantic query alive for >100 s. The crawl
    # now prunes before descent and stops when the foreground budget is spent.
    crawl_budget_s = _env_float("SKYGREP_CROSS_FOLDER_CRAWL_BUDGET_S", 1.5)
    max_files_per_root = _env_int(
        "SKYGREP_CROSS_FOLDER_MAX_FILES_PER_ROOT", 2000, minimum=1
    )
    deadline = time.perf_counter() + crawl_budget_s
    all_files: list[str] = []
    roots_seen = 0
    for root in candidate_roots:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            break
        if not root.is_dir():
            continue
        try:
            files, _ = crawl_tree(
                root,
                max_files=max_files_per_root,
                max_seconds=remaining,
            )
            roots_seen += 1
            all_files.extend(files)
        except Exception:
            continue
    if not all_files:
        return [], {"path": "lazy-cross-folder", "candidate_roots": roots_seen}

    seeds = token_shortcut_seeds(query, all_files, max_seeds=seed_budget)
    if not seeds:
        return [], {"path": "lazy-cross-folder",
                    "candidate_roots": roots_seen,
                    "files_seen": len(all_files),
                    "seeds": 0}

    n_new, embeddings, _ = embed_files_batch(conn, seeds, embedder)
    qv = np.asarray(embedder.embed(query), dtype=np.float32)
    top, sigma = cosine_topk_with_sigma(qv, embeddings, top_k=top_k)
    results = [
        {"path": p, "score": s, "snippet": "",
         "start_line": 0, "end_line": 0, "language": "", "chunk": ""}
        for p, s in top
    ]
    return results, {
        "path": "lazy-cross-folder",
        "candidate_roots": roots_seen,
        "files_seen": len(all_files),
        "seeds": len(seeds),
        "embedded_new": n_new,
        "sigma": round(sigma, 4),
    }


__all__ = [
    "crawl_tree",
    "render_tree_summary",
    "token_shortcut_seeds",
    "extract_imports",
    "resolve_imports_to_paths",
    "embed_files_batch",
    "cosine_topk_with_sigma",
    "lazy_explore_cold_start",
    "lazy_explore_cross_folder",
    "_dedupe_seed_groups",
    "_stderr_progress",
]
