"""Real-corpus end-to-end test for 0.4.0 holistic graph-aware retrieval.

Self-contained: indexes skylakegrep/src/ into a temp DB with real bge-m3
embeddings via Ollama, populates the reference graph (which writes to
graph_edge), then runs cascade_search forcing escalation (tau=0) so
the new ``_expand_via_reference_graph`` path always fires. Reports:

  * graph_edge population — did populate_graph_table actually write
    refs edges?
  * cascade telemetry on real semantic queries — does graph_expand
    appear in the telemetry, and how many candidates does it
    contribute?
  * top-5 results — does the answer file appear, and was it
    contributed by the cosine pool, the HyDE pool, or graph-expand?

Per the 2026-05-06 auto-memory rule
(``feedback_real_e2e_test_then_full_surface_update.md``): every release
needs a real CLI / real corpus run before ship.
"""

from __future__ import annotations
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.resolve()))
os.environ.setdefault("OLLAMA_URL", "http://localhost:11434")

from skylakegrep.src import storage as S
from skylakegrep.src.embeddings import OllamaEmbedder
from skylakegrep.src.answerer import OllamaAnswerer
from skylakegrep.src.reference_graph import populate_graph_table


REPO_ROOT = Path(__file__).parent.parent.resolve()
SRC_ROOT = REPO_ROOT / "skylakegrep" / "src"


# ── Build a fresh index from scratch ─────────────────────────────────
def build_fresh_index(db_path: Path) -> tuple[sqlite3.Connection, OllamaEmbedder]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = S.init_db(db_path)
    embedder = OllamaEmbedder(base_url="http://localhost:11434", model="bge-m3")

    files = sorted(p for p in SRC_ROOT.rglob("*.py")
                   if "__pycache__" not in str(p) and ".bak" not in str(p))

    print(f"[index ] Embedding {len(files)} Python source files…")
    t0 = time.perf_counter()
    chunks = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        # One chunk per file — coarse but enough for the bench
        emb = embedder.embed(text[:8000])
        chunks.append({
            "file": str(f), "chunk": text[:8000],
            "language": "python", "chunk_index": 0,
            "file_mtime": f.stat().st_mtime,
            "start_line": 1, "end_line": text.count("\n") + 1,
            "start_byte": 0, "end_byte": len(text),
            "embedding": list(emb),
        })
    S.store_chunks_batch(conn, chunks)
    # Populate file-level mean embeddings (cascade needs them)
    S.populate_file_embeddings(conn)
    conn.commit()

    # ── Build the v2 graph substrate (refs edges) ────────────────────
    print(f"[graph ] Building reference-graph edges…")
    populate_graph_table(conn, REPO_ROOT)
    n_edges = conn.execute(
        "SELECT COUNT(*) FROM graph_edge WHERE type='refs'").fetchone()[0]
    n_nodes = conn.execute("SELECT COUNT(*) FROM graph_node").fetchone()[0]
    print(f"        graph_node={n_nodes}  graph_edge[refs]={n_edges}  "
          f"build={(time.perf_counter()-t0)*1000:.0f}ms")
    return conn, embedder


# ── Test queries: deliberately semantic so cascade is likely to escalate ─
QUERIES = [
    ("how does the cascade decide whether to escalate to HyDE",   "storage.py"),
    ("how does proactive enhancement work after a low-confidence result", "proactive.py"),
    ("how is the LLM router decision cached",                     "llm_router.py"),
    ("how does the v2 graph expansion add candidates",            "storage.py"),
    ("how does symbol-aware ranking boost results",               "storage.py"),
]


def main() -> int:
    db = Path("/tmp/skg-real-bench.db")
    conn, embedder = build_fresh_index(db)
    answerer = OllamaAnswerer(base_url="http://localhost:11434",
                              model="qwen2.5:3b", hyde_model="qwen2.5:3b")

    print(f"\n{'='*80}\nReal-corpus cascade_search test "
          f"(tau=0.0 forces escalation, graph_expand expected)\n{'='*80}\n")

    fired = 0
    hits = 0
    for i, (q, expect) in enumerate(QUERIES, 1):
        qv = embedder.embed(q)
        t0 = time.perf_counter()
        results, telemetry = S.cascade_search(
            conn, qv, query_text=q, embedder=embedder, answerer=answerer,
            top_k=5, tau=0.0,           # force escalation
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        ge = telemetry.get("graph_expand")
        path = telemetry.get("path")
        top_paths = [Path(r.get("path", "")).name for r in results[:5]]
        hit = any(expect.lower() in p.lower() for p in top_paths)

        print(f"Q{i}. {q!r}")
        print(f"     elapsed: {elapsed_ms:7.0f} ms   path: {path}")
        if ge:
            fired += 1
            print(f"     ✓ graph_expand fired   "
                  f"seeds={ge.get('seeds'):2d}  "
                  f"neighbours_pulled={ge.get('neighbours_pulled'):3d}  "
                  f"scored={ge.get('scored'):3d}  "
                  f"kept={ge.get('kept'):3d}")
        else:
            print(f"     ✗ graph_expand did NOT fire (telemetry: {list(telemetry.keys())})")
        print(f"     top-5 files: {top_paths}")
        print(f"     expect '{expect}': {'✓ HIT' if hit else '✗ miss'}")
        if hit: hits += 1
        print()

    n = len(QUERIES)
    print("=" * 80)
    print(f"SUMMARY:  graph_expand fired on {fired}/{n} escalated queries")
    print(f"          top-5 hit rate: {hits}/{n} ({100*hits/n:.0f}%)")
    print("=" * 80)
    conn.close()
    return 0 if fired == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
