"""Real-corpus end-to-end test of 0.5.0 lazy index on a FRESH project.

The user's vision (from `memory/feedback_users_actual_vision.md`):
`skygrep search "<query>"` works on a project that has NEVER been
indexed, returning a useful answer in SECONDS. This bench measures
exactly that — time-to-first-answer with no prior `skygrep index .`
on a fresh Django checkout.

Acceptance:
  - Each query end-to-end < 30 s on cold project (vs. 5-10 min
    upfront index in 0.4.x).
  - Top-5 hit rate ≥ 7/10 on the Django benchmark fixture.
  - Embedded files per query < 200 (vs. ~5000 for full index).

Usage:
    rm -rf ~/.skylakegrep/repos/django-*    # ensure NO prior index
    .venv/bin/python benchmarks/release-0.5.0-lazy-index.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")

from skylakegrep.src import storage as S
from skylakegrep.src import lazy_indexer as LZ
from skylakegrep.src.embeddings import OllamaEmbedder
from skylakegrep.src.answerer import OllamaAnswerer


REPO = Path("/tmp/oss-bench/django")
TASKS_FILE = Path(__file__).parent / "cross_repo" / "django.json"


def main() -> int:
    if not REPO.exists():
        print(f"ERROR: Django not at {REPO}. clone first:")
        print(f"  git clone --depth=1 https://github.com/django/django {REPO}")
        return 2

    tasks = json.loads(TASKS_FILE.read_text())
    print(f"{'='*82}")
    print(f"skylakegrep 0.5.0 LAZY-INDEX bench")
    print(f"  corpus: {REPO}")
    print(f"  tasks:  {TASKS_FILE} ({len(tasks)} questions)")
    print(f"{'='*82}\n")

    # Fresh DB — simulate "this project has never been indexed"
    db_path = Path("/tmp/skg-lazy-django.db")
    if db_path.exists():
        db_path.unlink()
    conn = S.init_db(db_path)
    embedder = OllamaEmbedder(base_url="http://localhost:11434", model="bge-m3")
    answerer = OllamaAnswerer(base_url="http://localhost:11434",
                              model="qwen2.5:3b", hyde_model="qwen2.5:3b")

    hits = 0
    n = len(tasks)
    total_embedded = 0
    elapsed_per_query: list[float] = []
    for i, task in enumerate(tasks, 1):
        question = task["question"]
        expected = task["expected"]
        alts = task.get("expected_alternatives", [])
        all_acceptable = [expected] + alts

        t0 = time.perf_counter()
        results, tele = LZ.lazy_explore_cold_start(
            conn, question, REPO, embedder, top_k=5,
        )
        elapsed = time.perf_counter() - t0
        elapsed_per_query.append(elapsed)
        embedded_this_query = (
            tele.get("embed_new", 0)
        )
        total_embedded += embedded_this_query

        top_paths = [r.get("path", "") for r in results[:5]]
        # Match by suffix (results are absolute paths, expected is repo-relative)
        hit = any(any(p.endswith("/" + acc) or p.endswith(acc) for p in top_paths)
                  for acc in all_acceptable)
        if hit:
            hits += 1
        marker = "✓" if hit else "✗"

        print(f"Q{i:2d}. {question[:70]!r}")
        print(f"     elapsed: {elapsed:5.1f}s  embedded(new/cached): "
              f"{tele.get('embed_new', 0)}/{tele.get('embed_cached', 0)}"
              f"  σ={tele.get('sigma', 0)}  conf={tele.get('confidence', '?')}")
        print(f"     top-5: {[Path(p).name for p in top_paths]}")
        print(f"     expect '{expected}': {marker}")
        print()

    n_safe = max(n, 1)
    p50 = sorted(elapsed_per_query)[n // 2] if elapsed_per_query else 0
    p_max = max(elapsed_per_query) if elapsed_per_query else 0
    print(f"{'='*82}")
    print(f"SUMMARY:  hits {hits}/{n} ({100*hits//n_safe}%)")
    print(f"          latency p50={p50:.1f}s  max={p_max:.1f}s")
    print(f"          total files embedded across run: {total_embedded}")
    print(f"          (Django has ~5000 source files; full upfront index "
          f"would cost ~5000 embeddings)")
    print(f"{'='*82}")
    conn.close()
    return 0 if hits >= 7 and p_max <= 30 else 1


if __name__ == "__main__":
    raise SystemExit(main())
