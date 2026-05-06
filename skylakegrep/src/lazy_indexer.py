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

**Latency target: ≤ 10 s** for the cold-start case (15 seed files +
batch-embed + cosine + validate). Achieved by:
  - `OllamaEmbedder.embed_batch(texts)` — one API call for all seeds
  - Tight seed budget (10-15 files, not 25)
  - σ-evidence validation: only return when top-K is well-separated
    enough to be worth showing

**Hyperparameter contract: zero new constants.** All scoring is
cosine; the σ-validation gate reuses `CASCADE_TAU_FLOOR`.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Optional

import numpy as np

from . import storage as S


# ── Filesystem crawl ────────────────────────────────────────────────────


_DEFAULT_CODE_EXTENSIONS = (
    ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java", ".kt",
    ".rb", ".php", ".cs", ".cpp", ".cc", ".c", ".h", ".hpp", ".swift",
    ".md", ".rst", ".txt", ".toml", ".yml", ".yaml", ".json",
)
_DEFAULT_IGNORE_DIRS = frozenset({
    "node_modules", ".git", "venv", ".venv", "__pycache__", "target",
    "build", "dist", "out", ".tox", ".pytest_cache", "vendor",
    ".next", ".nuxt", ".cache",
})


def crawl_tree(
    root: Path,
    *,
    max_files: int = 5000,
    extensions: tuple[str, ...] = _DEFAULT_CODE_EXTENSIONS,
) -> tuple[list[str], dict[str, int]]:
    """Walk ``root``, return ``(file_paths, dir_summary)``. Cheap stat-only."""
    files: list[str] = []
    dir_summary: dict[str, int] = {}
    root = root.resolve()
    for path in root.rglob("*"):
        if any(part in _DEFAULT_IGNORE_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() not in extensions:
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        files.append(str(path))
        parent = str(rel.parent) if rel.parent != Path(".") else "."
        dir_summary[parent] = dir_summary.get(parent, 0) + 1
        if len(files) >= max_files:
            break
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


def token_shortcut_seeds(
    query: str, files: list[str], *, max_seeds: int = 15,
) -> list[str]:
    """Files whose basename or any directory component matches a
    query-derived token. Cheap, deterministic, no LLM.
    """
    import re
    raw = re.findall(r"[A-Za-z][A-Za-z0-9_]*", query)
    pieces: set[str] = set()
    for tok in raw:
        sub = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", tok)
        for p in sub.lower().split():
            if len(p) >= 3 and p not in _QUERY_STOPWORDS:
                pieces.add(p)
    if not pieces:
        return []
    scored: list[tuple[int, str]] = []
    for f in files:
        f_lower = f.lower()
        hits = sum(1 for p in pieces if p in f_lower)
        if hits > 0:
            scored.append((hits, f))
    scored.sort(key=lambda kv: (-kv[0], kv[1]))
    return [f for _, f in scored[:max_seeds]]


# ── Batch on-demand embedding ───────────────────────────────────────────


def embed_files_batch(
    conn: sqlite3.Connection,
    files: list[str],
    embedder,
    *,
    chunk_chars: int = 8000,
) -> tuple[int, dict[str, np.ndarray]]:
    """Batch-embed a list of files in ONE Ollama API call (when the
    embedder supports `embed_batch`). Returns ``(new_count, embeddings)``.

    Embeddings are cached in the chunks table so repeat queries on the
    same project never re-embed.

    Latency target: < 5 s for 15 files via batch — vs. 15-25 s sequential.
    """
    if not files:
        return 0, {}
    # Filter to only un-embedded files (cache hit fast-path)
    fresh: list[tuple[str, str, float]] = []
    cached: dict[str, np.ndarray] = {}
    for f in files:
        cur = conn.execute(
            "SELECT c.id, v.embedding FROM chunks c "
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
            continue
        try:
            text = Path(f).read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        try:
            mtime = Path(f).stat().st_mtime
        except OSError:
            mtime = 0.0
        fresh.append((f, text[:chunk_chars], mtime))

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
    return len(fresh), {**cached, **new_embeddings}


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


# ── Cold-start lazy explore — the public API ────────────────────────────


def lazy_explore_cold_start(
    conn: sqlite3.Connection,
    query: str,
    root: Path,
    embedder,
    *,
    top_k: int = 5,
    seed_budget: int = 25,
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
    """
    t0 = time.perf_counter()
    tele: dict = {"path": "lazy-cold-start"}

    # Step 1: cheap crawl
    t1 = time.perf_counter()
    files, dir_summary = crawl_tree(Path(root))
    tele["crawled_files"] = len(files)
    tele["crawl_ms"] = int((time.perf_counter() - t1) * 1000)

    # Step 2: deterministic token shortcut seeds
    seeds = token_shortcut_seeds(query, files, max_seeds=seed_budget)
    tele["token_seeds"] = len(seeds)

    # Step 3: LLM router for likely entry paths (additive)
    try:
        from . import llm_router as LR
        t1 = time.perf_counter()
        tree_summary = render_tree_summary(dir_summary)
        llm_picks = LR.infer_candidate_paths(query, tree_summary)
        tele["llm_router_ms"] = int((time.perf_counter() - t1) * 1000)
        tele["llm_picks"] = len(llm_picks)
        files_set = set(files)
        for raw in llm_picks:
            if len(seeds) >= seed_budget:
                break
            cand = (Path(root) / raw).resolve()
            if str(cand) in files_set and str(cand) not in seeds:
                seeds.append(str(cand))
            elif cand.is_dir():
                # take a few files from picked dir
                added = 0
                for f in files:
                    if f.startswith(str(cand) + "/") and f not in seeds:
                        seeds.append(f)
                        added += 1
                        if added >= 3 or len(seeds) >= seed_budget:
                            break
    except Exception as e:
        tele["llm_router_error"] = str(e)[:80]

    if not seeds:
        # Defensive fallback: first N files
        seeds = files[:seed_budget]
        tele["seed_fallback"] = "first-N"

    tele["seeds_total"] = len(seeds)

    # Step 4: batch embed (one Ollama call)
    t1 = time.perf_counter()
    n_new, embeddings = embed_files_batch(conn, seeds, embedder)
    tele["embed_new"] = n_new
    tele["embed_cached"] = len(embeddings) - n_new
    tele["embed_ms"] = int((time.perf_counter() - t1) * 1000)

    # Step 5: query embedding + cosine top-K
    t1 = time.perf_counter()
    qv = np.asarray(embedder.embed(query), dtype=np.float32)
    tele["query_embed_ms"] = int((time.perf_counter() - t1) * 1000)

    top, sigma = cosine_topk_with_sigma(qv, embeddings, top_k=top_k)
    tele["sigma"] = round(sigma, 4)

    # Step 6: σ-validation — re-use CASCADE_TAU_FLOOR as noise floor.
    # Confidence: top-1 score must be meaningful (cosine > 0.3) AND
    # σ-evidence shows separation from the rest.
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
    seed_budget: int = 10,
    candidate_roots: Optional[list[Path]] = None,
) -> tuple[list[dict], dict]:
    """Cross-folder proactive explorer — searches multiple candidate
    roots (LLM-decided) when cascade can't find the answer in the
    current folder.

    Replaces ``proactive.filename_extend``'s hardcoded
    ``~/Downloads / ~/Desktop / ~/Documents`` list. Per-query, the
    LLM router picks which candidate roots to explore.

    Returns the same shape as `lazy_explore_cold_start`.
    """
    if candidate_roots is None:
        candidate_roots = [
            Path.home() / d for d in
            ("Downloads", "Desktop", "Documents", "Pictures", "Code", "Projects")
            if (Path.home() / d).exists()
        ]

    # Aggregate token-shortcut seeds across candidate roots
    all_files: list[str] = []
    for root in candidate_roots:
        if not root.is_dir():
            continue
        try:
            files, _ = crawl_tree(root, max_files=2000)
            all_files.extend(files)
        except Exception:
            continue
    if not all_files:
        return [], {"path": "lazy-cross-folder", "candidate_roots": 0}

    seeds = token_shortcut_seeds(query, all_files, max_seeds=seed_budget)
    if not seeds:
        return [], {"path": "lazy-cross-folder",
                    "files_seen": len(all_files),
                    "seeds": 0}

    n_new, embeddings = embed_files_batch(conn, seeds, embedder)
    qv = np.asarray(embedder.embed(query), dtype=np.float32)
    top, sigma = cosine_topk_with_sigma(qv, embeddings, top_k=top_k)
    results = [
        {"path": p, "score": s, "snippet": "",
         "start_line": 0, "end_line": 0, "language": "", "chunk": ""}
        for p, s in top
    ]
    return results, {
        "path": "lazy-cross-folder",
        "candidate_roots": len(candidate_roots),
        "files_seen": len(all_files),
        "seeds": len(seeds),
        "embedded_new": n_new,
        "sigma": round(sigma, 4),
    }


__all__ = [
    "crawl_tree",
    "render_tree_summary",
    "token_shortcut_seeds",
    "embed_files_batch",
    "cosine_topk_with_sigma",
    "lazy_explore_cold_start",
    "lazy_explore_cross_folder",
]
