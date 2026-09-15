# SPDX-License-Identifier: Apache-2.0
"""Mixedbread `@mixedbread/mgrep` cloud vs skylakegrep parity harness.

Measures retrieval parity between Mixedbread's cloud-backed CLI
(<https://github.com/mixedbread-ai/mgrep>, npm `@mixedbread/mgrep`) and
this project's fully local implementation. It is the only parity benchmark
in this repository that requires a third-party account and cloud upload.

skylakegrep is an independent implementation, not a fork: mgrep is
TypeScript on npm and talks to a paid hosted index, this is Python
talking to a local SQLite index and a local Ollama. The comparison is
between two designs with the same query surface and opposite trade-offs,
which is the only reason a parity harness is interesting at all.

PREREQUISITES (one-time, manual)
--------------------------------
1. Install the Mixedbread CLI. Its binary is named ``mgrep``, so it does
   not collide with this project's ``skygrep``:

       npm install -g @mixedbread/mgrep
       # or, to keep it out of the global prefix:
       mkdir -p ~/.local/share/mixedbread-mgrep && cd $_
       npm init -y
       npm install @mixedbread/mgrep

2. Log in. The CLI uses an interactive device/OAuth flow:

       <path-to>/node_modules/.bin/mgrep login

3. Sync the target repository to a Mixedbread store:

       cd /path/to/repo
       <path-to>/node_modules/.bin/mgrep watch

   This uploads the repository contents to Mixedbread cloud for
   indexing, billed per content token. Free-tier quotas apply.

4. Confirm authentication is healthy by running a search interactively
   and seeing results:

       <path-to>/node_modules/.bin/mgrep "..."

Once these prerequisites are satisfied, point this script at the same
repository and the same task list used by ``parity_vs_ripgrep.py`` to
get a side-by-side comparison.

USAGE
-----
    .venv/bin/python benchmarks/parity_vs_mixedbread.py \
        --root /path/to/repo \
        --tasks benchmarks/cross_repo/rust-workspace.json \
        --mixedbread-bin /path/to/mixedbread-skygrep/node_modules/.bin/skygrep \
        --top-k 10

LIMITATIONS
-----------
- Mixedbread skygrep is a cloud service. The repository contents are
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
    skygrep_agent_context,
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
from skylakegrep.src.indexer import collect_indexable_files


# Heuristic for parsing Mixedbread skygrep stdout. The CLI emits something
# along the lines of ``relative/path.ext:line: surrounding text``; we
# extract the path token at the start of each line. If you see paths
# being missed, log the raw stdout and tighten this regex.
PATH_LINE_RE = re.compile(r"^\s*([^\s:]+\.[A-Za-z0-9]+):(\d+)")


def parse_mixedbread_output(stdout: str) -> tuple[list[str], int]:
    """Best-effort parse of Mixedbread skygrep stdout into (paths, char_count).

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
            "Mixedbread CLI not found. Install via npm and pass\n"
            "--mixedbread-bin /path/to/node_modules/.bin/mgrep, or put it\n"
            "on PATH. See module docstring for one-time setup steps."
        )
    # Integrity guard. The control arm must not be this project. Mixedbread's
    # binary is named `mgrep`; ours is `skygrep`, so any binary with our name
    # is disqualified no matter where it lives. A previous version of this
    # check only rejected one hardcoded Homebrew path, which meant a `skygrep`
    # earlier on PATH — a venv, /usr/local/bin, a shim — silently produced a
    # benchmark of skylakegrep against itself, reporting perfect parity.
    if Path(mxbread_bin).name in {"skygrep", "skygrep.exe"}:
        sys.exit(
            f"Refusing to use {mxbread_bin}: `skygrep` is this project's own\n"
            "binary, not the Mixedbread CLI, and comparing it with itself\n"
            "would report parity by construction. Pass --mixedbread-bin\n"
            "pointing at a separate Mixedbread install (binary name `mgrep`)."
        )

    root = Path(args.root).resolve()
    db_path = (
        Path(args.db_path)
        if args.db_path
        else Path(tempfile.gettempdir()) / "skylakegrep-mixedbread-parity.sqlite"
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
        skygrep_result = skygrep_agent_context(
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
                "skylakegrep_local": {
                    **skygrep_result,
                    "hit": expected_hit(expected, skygrep_result["paths"]),
                },
                "context_token_reduction_x_local_vs_cloud": safe_ratio(
                    float(mxbread_result["context_tokens"]),
                    float(skygrep_result["context_tokens"]),
                ),
            }
        )

    mxb_hits = sum(1 for r in rows if r["mixedbread"]["hit"])
    local_hits = sum(1 for r in rows if r["skylakegrep_local"]["hit"])

    return {
        "definition": {
            "benchmark_type": "Mixedbread cloud skygrep vs skylakegrep retrieval parity",
            "mixedbread_agent": "one Mixedbread `skygrep search` per task (cloud embeddings, paid quota)",
            "local_agent": "one skylakegrep semantic top-k search per task (Ollama embeddings)",
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
            "skylakegrep_local_hit_rate": f"{local_hits}/{len(rows)}",
            "agreement": sum(
                1
                for r in rows
                if r["mixedbread"]["hit"] == r["skylakegrep_local"]["hit"]
            ),
            "mixedbread_only_hits": sum(
                1
                for r in rows
                if r["mixedbread"]["hit"] and not r["skylakegrep_local"]["hit"]
            ),
            "skylakegrep_local_only_hits": sum(
                1
                for r in rows
                if r["skylakegrep_local"]["hit"] and not r["mixedbread"]["hit"]
            ),
            "skylakegrep_local_avg_latency_seconds": round(
                sum(float(r["skylakegrep_local"]["latency_seconds"]) for r in rows)
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
        description="Benchmark Mixedbread cloud skygrep vs skylakegrep on the same task set."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--db-path")
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--chars-per-token", type=int, default=4)
    parser.add_argument(
        "--mixedbread-bin",
        help="Path to the Mixedbread `mgrep` binary. If omitted, uses `mgrep` from PATH; "
        "the harness refuses any binary named `skygrep`, which would be this project.",
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
