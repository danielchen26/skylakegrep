"""Enrich only the chunks under crates/ai/ and app/src/billing/ in the repo-A
index — the two paths the repo-A 16-task benchmark currently misses with
14/16 recall.

If targeted enrichment of those two directories flips the bench to 15 or
16 / 16, we have a clean signal that L3 doc2query is what's needed for
the residual hard-miss queries (and ColBERT / fine-tune are not).

Run:
    OLLAMA_EMBED_MODEL=nomic-embed-text MGREP_DB_PATH=/tmp/<repo-D>_idx_p1.db \
      .venv/bin/python benchmarks/v0_5_targeted_enrich.py
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path("/Users/tianchichen/Documents/github/local-mgrep")
sys.path.insert(0, str(REPO))

os.environ.setdefault("OLLAMA_EMBED_MODEL", "nomic-embed-text")
os.environ.setdefault("MGREP_DB_PATH", "/tmp/<repo-D>_idx_p1.db")

import numpy as np
import requests

from local_mgrep.src.config import get_config
from local_mgrep.src.embeddings import get_embedder

DB = "/tmp/<repo-D>_idx_p1.db"

PATTERNS = ("%/crates/ai/%", "%/app/src/billing/%")


def describe(text: str, path: str, language: str) -> str:
    cfg = get_config()
    prompt = (
        "Write a one or two sentence high-level description of what this "
        "code does, focusing on user-facing concepts (e.g. \"auth\", "
        "\"billing\", \"language model backend\") rather than "
        "implementation detail. Output only the description, no "
        "preamble, no markdown.\n\n"
        f"File: {path}\n"
        f"Language: {language}\n\n"
        f"```{language}\n{text}\n```"
    )
    try:
        r = requests.post(
            f"{cfg['ollama_url'].rstrip('/')}/api/generate",
            json={
                "model": cfg["llm_model"],
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0, "seed": 42, "num_predict": 96},
            },
            timeout=120,
        )
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except requests.RequestException as exc:
        print(f"  ! {path}: {exc}", file=sys.stderr)
        return ""


def main() -> None:
    conn = sqlite3.connect(DB)
    embedder = get_embedder()  # document-side embeddings

    rows = []
    for pat in PATTERNS:
        rows.extend(
            conn.execute(
                "SELECT id, file, chunk, language FROM chunks "
                "WHERE (file LIKE ?) AND enriched_at IS NULL",
                (pat,),
            ).fetchall()
        )
    print(f"targeted enrich: {len(rows)} chunks pending", flush=True)
    if not rows:
        return

    t0 = time.time()
    done = 0
    for chunk_id, path, text, language in rows:
        desc = describe(text, path, language or "")
        if not desc:
            continue
        augmented = f"{text}\n\n{desc}"
        new_vec = np.array(embedder.embed(augmented), dtype=np.float32)
        conn.execute(
            "UPDATE chunks SET description = ?, enriched_at = ? WHERE id = ?",
            (desc, time.time(), chunk_id),
        )
        conn.execute(
            "UPDATE vectors SET embedding = ? WHERE id = ?",
            (new_vec.tobytes(), chunk_id),
        )
        conn.commit()
        done += 1
        if done % 25 == 0 or done == len(rows):
            print(f"  · {done}/{len(rows)} chunks · {time.time() - t0:.1f}s", flush=True)
    print(f"\n✓ done · {done} chunks enriched in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
