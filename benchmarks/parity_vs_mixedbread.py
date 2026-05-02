"""Mixedbread `@mixedbread/mgrep` cloud vs local-mgrep parity harness.

This script measures retrieval parity between the cloud Mixedbread mgrep
(the original project at <https://github.com/mixedbread-ai/mgrep>) and
this fork's fully local implementation. It is the only parity benchmark
in this repository that requires a third-party account and cloud upload.

PREREQUISITES (one-time, manual)
--------------------------------
1. Install the Mixedbread CLI somewhere reachable, e.g.

       npm install -g @mixedbread/mgrep
       # or (preferred, doesn't conflict with this repo's own /opt/homebrew/bin/mgrep
       # wrapper) install into a project-local node_modules:
       mkdir -p ~/.local/share/mixedbread-mgrep && cd $_
       npm init -y
       npm install @mixedbread/mgrep

2. Log in. The CLI uses an interactive OAuth flow:

       <path-to>/node_modules/.bin/mgrep login

3. Sync the target repository to a Mixedbread store. The default store
   name is ``mgrep``; pass ``--store NAME`` to use a separate one:

       cd /path/to/repo
       <path-to>/node_modules/.bin/mgrep search "test query" . --sync

   This uploads the repository contents to Mixedbread cloud for
   indexing. Free-tier quotas apply.

4. Confirm authentication is healthy by running a search interactively
   and seeing results:

       <path-to>/node_modules/.bin/mgrep search "..." . -c

Once these prerequisites are satisfied, point this script at the same
repository and the same task list used by ``parity_vs_ripgrep.py`` to
get a side-by-side comparison.

USAGE
-----
    .venv/bin/python benchmarks/parity_vs_mixedbread.py \
        --root /path/to/repo \
        --tasks benchmarks/cross_repo/warp.json \
        --mixedbread-bin /Users/me/.local/share/mixedbread-mgrep/node_modules/.bin/mgrep \
        --top-k 10

LIMITATIONS
-----------
- Mixedbread mgrep is a cloud service. The repository contents are
  uploaded to Mixedbread before the first search. This is an explicit
  privacy trade-off; do not run this benchmark on private code unless
  the upload is acceptable.
- The Mixedbread CLI's stdout format is not a stable JSON contract. We
  parse it best-effort by scanning for ``path:line:`` style fences. If
  the format changes, update ``parse_mixedbread_output`` and re-run.
- No fixed prompt/answer overhead model is added here — the script just
  reports retrieval-layer metrics (paths, hit, context tokens, latency).
  Combine with ``parity_vs_ripgrep.py``'s output if a total-token
  comparison is needed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.agent_context_benchmark import (
    DEFAULT_TASKS,
    mgrep_agent_context,
    safe_ratio,
)
from benchmarks.parity_vs_ripgrep import expected_hit
from benchmarks.token_savings import (
    approximate_tokens,
    build_index,
    collect_source_doc_files,
    count_files,
    is_benchmark_ignored,
)
from local_mgrep.src.indexer import collect_indexable_files


# Heuristic for parsing Mixedbread mgrep stdout. The CLI emits something
# along the lines of ``relative/path.ext:line: surrounding text``; we
# extract the path token at the start of each line. If you see paths
# being missed, log the raw stdout and tighten this regex.
PATH_LINE_RE = re.compile(r"^\s*([^\s:]+\.[A-Za-z0-9]+):(\d+)")


def parse_mixedbread_output(stdout: str) -> tuple[list[str], int]:
    """Best-effort parse of Mixedbread mgrep stdout into (paths, char_count).

    Returns the list of unique paths in encounter order plus the total
    character count of the parsed output (used to estimate context
    tokens).
    """
    paths: list[str] = []
    seen: set[str] = set()
    for raw_line in stdout.splitlines():
        match = PATH_LINE_RE.match(raw_line)
        if not match:
            continue
        path = match.group(1)
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths, len(stdout)


def mixedbread_agent_context(
    mxbread_bin: str,
    question: str,
    repo: Path,
    top_k: int,
    chars_per_token: int,
    extra_args: list[str],
) -> dict[str, object]:
    cmd = [
        mxbread_bin,
        "search",
        question,
        str(repo),
        "-c",            # include content snippets
        "-m", str(top_k),
    ] + extra_args
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180,
            env={**os.environ},
        )
        stdout = proc.stdout
        stderr = proc.stderr
    except subprocess.TimeoutExpired:
        return {
            "tool_calls": 1,
            "paths": [],
            "context_chars": 0,
            "context_tokens": 0,
            "latency_seconds": 180.0,
            "error": "timeout",
        }
    paths, chars = parse_mixedbread_output(stdout)
    return {
        "tool_calls": 1,
        "paths": paths,
        "context_chars": chars,
        "context_tokens": approximate_tokens(stdout, chars_per_token),
        "latency_seconds": round(time.perf_counter() - started, 3),
        "stderr_excerpt": stderr.strip()[:200] if stderr else None,
    }


def load_tasks(path: Path | None) -> list[dict[str, str]]:
    if path is None:
        return DEFAULT_TASKS
    return json.loads(path.read_text(encoding="utf-8"))


def benchmark(args: argparse.Namespace) -> dict[str, object]:
    mxbread_bin = args.mixedbread_bin or shutil.which("mgrep")
    if not mxbread_bin:
        sys.exit(
            "Mixedbread mgrep CLI not found. Install via npm and pass\n"
            "--mixedbread-bin /path/to/node_modules/.bin/mgrep, or put it\n"
            "on PATH. See module docstring for one-time setup steps."
        )
    if Path(mxbread_bin).resolve() == Path("/opt/homebrew/bin/mgrep").resolve():
        sys.exit(
            f"Refusing to use {mxbread_bin}: that path is the local-mgrep\n"
            "wrapper installed by this repo, not the Mixedbread CLI.\n"
            "Pass --mixedbread-bin pointing at a separate Mixedbread install."
        )

    root = Path(args.root).resolve()
    db_path = (
        Path(args.db_path)
        if args.db_path
        else Path(tempfile.gettempdir()) / "local-mgrep-mixedbread-parity.sqlite"
    )

    indexed_files = [
        p for p in collect_indexable_files(root) if not is_benchmark_ignored(p, root)
    ]
    indexed_corpus = count_files(indexed_files, args.chars_per_token)

    conn, index_seconds = build_index(root, db_path, batch_size=args.batch_size)
    chunks, indexed_db_files = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT file) FROM chunks"
    ).fetchone()

    extra_args = []
    if args.mixedbread_store:
        extra_args = ["--store", args.mixedbread_store, *extra_args]
    if args.mixedbread_sync:
        extra_args.append("--sync")

    rows: list[dict[str, object]] = []
    for task in load_tasks(args.tasks):
        expected = task["expected"]
        mxbread_result = mixedbread_agent_context(
            mxbread_bin,
            task["question"],
            root,
            top_k=args.top_k,
            chars_per_token=args.chars_per_token,
            extra_args=extra_args,
        )
        mgrep_result = mgrep_agent_context(
            conn,
            task["question"],
            top_k=args.top_k,
            chars_per_token=args.chars_per_token,
        )
        rows.append(
            {
                "id": task["id"],
                "question": task["question"],
                "expected": expected,
                "mixedbread": {
                    **mxbread_result,
                    "hit": expected_hit(expected, mxbread_result["paths"]),
                },
                "mgrep_local": {
                    **mgrep_result,
                    "hit": expected_hit(expected, mgrep_result["paths"]),
                },
                "context_token_reduction_x_local_vs_cloud": safe_ratio(
                    float(mxbread_result["context_tokens"]),
                    float(mgrep_result["context_tokens"]),
                ),
            }
        )

    mxb_hits = sum(1 for r in rows if r["mixedbread"]["hit"])
    local_hits = sum(1 for r in rows if r["mgrep_local"]["hit"])

    return {
        "definition": {
            "benchmark_type": "Mixedbread cloud mgrep vs local-mgrep retrieval parity",
            "mixedbread_agent": "one Mixedbread `mgrep search` per task (cloud embeddings, paid quota)",
            "local_agent": "one local-mgrep semantic top-k search per task (Ollama embeddings)",
            "note": "Both sides use the same task questions and expected files.",
        },
        "tooling": {
            "mixedbread_bin": str(mxbread_bin),
            "mixedbread_version": subprocess.run(
                [mxbread_bin, "-V"], capture_output=True, text=True
            ).stdout.strip(),
        },
        "parameters": {
            "tasks": len(rows),
            "top_k": args.top_k,
            "mixedbread_store": args.mixedbread_store,
            "mixedbread_sync": args.mixedbread_sync,
        },
        "index": {
            "seconds": round(index_seconds, 3),
            "db_path": str(db_path),
            "indexed_db_files": indexed_db_files,
            "chunks": chunks,
            "indexed_corpus": indexed_corpus,
        },
        "summary": {
            "mixedbread_hit_rate": f"{mxb_hits}/{len(rows)}",
            "mgrep_local_hit_rate": f"{local_hits}/{len(rows)}",
            "agreement": sum(
                1
                for r in rows
                if r["mixedbread"]["hit"] == r["mgrep_local"]["hit"]
            ),
            "mixedbread_only_hits": sum(
                1
                for r in rows
                if r["mixedbread"]["hit"] and not r["mgrep_local"]["hit"]
            ),
            "mgrep_local_only_hits": sum(
                1
                for r in rows
                if r["mgrep_local"]["hit"] and not r["mixedbread"]["hit"]
            ),
            "mgrep_local_avg_latency_seconds": round(
                sum(float(r["mgrep_local"]["latency_seconds"]) for r in rows)
                / len(rows),
                3,
            ),
            "mixedbread_avg_latency_seconds": round(
                sum(float(r["mixedbread"]["latency_seconds"]) for r in rows)
                / len(rows),
                3,
            ),
        },
        "tasks": rows,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark Mixedbread cloud mgrep vs local-mgrep on the same task set."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--db-path")
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--chars-per-token", type=int, default=4)
    parser.add_argument(
        "--mixedbread-bin",
        help="Path to the Mixedbread mgrep binary. If omitted, uses `mgrep` from PATH; "
        "the harness refuses to run if that resolves to the local-mgrep wrapper.",
    )
    parser.add_argument(
        "--mixedbread-store",
        help="Mixedbread store name (passed as `--store`). Optional.",
    )
    parser.add_argument(
        "--mixedbread-sync",
        action="store_true",
        help="Pass `--sync` so Mixedbread re-uploads files before searching.",
    )
    parser.add_argument(
        "--summary-only",
        action="store_true",
        help="Only print definition, tooling, parameters, index, and summary",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = benchmark(args)
    if args.summary_only:
        report = {
            key: report[key]
            for key in ("definition", "tooling", "parameters", "index", "summary")
        }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
