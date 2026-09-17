"""Mixedbread `@mixedbread/mgrep` cloud vs skylakegrep parity harness.

This script measures retrieval parity between Mixedbread's cloud-backed
``mgrep`` CLI (see <https://github.com/mixedbread-ai/mgrep> /
``@mixedbread/mgrep``) and skylakegrep's fully local implementation. It is
the only parity benchmark in this repository that requires a third-party
account and cloud upload.

PREREQUISITES (one-time, manual)
--------------------------------
1. Install Mixedbread's ``mgrep`` CLI somewhere reachable (package:
   ``@mixedbread/mgrep`` on npm). Prefer a path that does **not** collide
   with this repo's ``skygrep`` console script.

2. Authenticate with Mixedbread per that CLI's docs (login / API key).

3. Sync the target repository to a Mixedbread store if the CLI requires
   it for search (store name / ``--sync`` flags vary by CLI version).

4. Confirm authentication is healthy by running a search interactively
   and seeing results.

Once these prerequisites are satisfied, pass ``--mixedbread-bin`` pointing
at that Mixedbread binary (or ensure ``mgrep`` is on PATH) and point this
script at the same repository and task list used by
``parity_vs_ripgrep.py`` for a side-by-side comparison.

USAGE
-----
    .venv/bin/python benchmarks/parity_vs_mixedbread.py \
        --root /path/to/repo \
        --tasks benchmarks/cross_repo/rust-workspace.json \
        --mixedbread-bin /path/to/node_modules/.bin/mgrep \
        --top-k 10

The harness never treats PATH ``skygrep`` as the Mixedbread baseline: that
name is this package's entry point, and using it would silently self-compare.

LIMITATIONS
-----------
- Mixedbread ``mgrep`` is a cloud-backed service. Repository contents may be
  uploaded to Mixedbread before search. This is an explicit privacy
  trade-off; do not run this benchmark on private code unless the upload
  is acceptable.
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


# Heuristic for parsing Mixedbread mgrep stdout. The CLI emits something
# along the lines of ``relative/path.ext:line: surrounding text``; we
# extract the path token at the start of each line. If you see paths
# being missed, log the raw stdout and tighten this regex.
PATH_LINE_RE = re.compile(r"^\s*([^\s:]+\.[A-Za-z0-9]+):(\d+)")


# Markers that identify *this* package's console-script / wrapper, not Mixedbread.
_SKYLAKEGREP_ENTRY_MARKERS = (
    "from skylakegrep.src.cli import main",
    "skylakegrep.src.cli:main",
    "skylakegrep.src.cli",
)

# Known install path for this repo's Homebrew-style wrapper (historical).
_KNOWN_LOCAL_SKYGREP_WRAPPERS = (
    Path("/opt/homebrew/bin/skygrep"),
)


def is_skylakegrep_entry(bin_path: Path) -> bool:
    """Return True if *bin_path* is this package's ``skygrep`` CLI entry.

    Used to fail-closed when the Mixedbread baseline would otherwise resolve
    to the local skylakegrep binary (silent self-compare).
    """
    try:
        resolved = bin_path.expanduser().resolve()
    except OSError:
        resolved = bin_path.expanduser()

    for known in _KNOWN_LOCAL_SKYGREP_WRAPPERS:
        try:
            if known.exists() and resolved == known.resolve():
                return True
        except OSError:
            continue

    if _path_content_is_skylakegrep(resolved):
        return True

    # Same path as PATH ``skygrep`` and not proven Mixedbread → fail closed.
    which_skygrep = shutil.which("skygrep")
    if which_skygrep:
        try:
            if resolved == Path(which_skygrep).resolve():
                return not _path_content_is_mixedbread(resolved)
        except OSError:
            return True

    return False


def _read_bin_head(bin_path: Path, limit: int = 8192) -> str:
    try:
        return bin_path.read_text(encoding="utf-8", errors="ignore")[:limit]
    except (OSError, UnicodeError):
        return ""


def _path_content_is_skylakegrep(bin_path: Path) -> bool:
    text = _read_bin_head(bin_path)
    return any(marker in text for marker in _SKYLAKEGREP_ENTRY_MARKERS)


def _path_content_is_mixedbread(bin_path: Path) -> bool:
    text = _read_bin_head(bin_path).lower()
    if not text:
        return False
    return (
        "@mixedbread/mgrep" in text
        or "mixedbread-ai/mgrep" in text
        or ("mixedbread" in text and "mgrep" in text)
    )


def resolve_mixedbread_bin(explicit: str | None) -> str:
    """Resolve the Mixedbread baseline binary, fail-closed on self-compare.

    - Prefer an explicit ``--mixedbread-bin``.
    - Otherwise accept PATH ``mgrep`` only (Mixedbread's real CLI name).
    - Never fall back to PATH ``skygrep`` (this package's entry point).
    - If the resolved path is this package's skygrep / skylakegrep entry,
      raise ``SystemExit`` with a clear error (non-zero).
    """
    if explicit:
        candidate = explicit
    else:
        candidate = shutil.which("mgrep")
        if not candidate:
            raise SystemExit(
                "Mixedbread CLI not found. Pass --mixedbread-bin pointing at\n"
                "the Mixedbread `mgrep` binary (from @mixedbread/mgrep), or put\n"
                "`mgrep` on PATH. PATH fallback to `skygrep` is refused — that\n"
                "name is this package's entry point and would silently self-compare.\n"
                "See module docstring for one-time setup steps."
            )

    # Allow bare command names via PATH when an explicit value was a name.
    path = Path(candidate).expanduser()
    if not path.is_file():
        found = shutil.which(candidate)
        if found:
            path = Path(found)
    if not path.is_file():
        raise SystemExit(
            f"Mixedbread binary not found: {candidate}\n"
            "Pass --mixedbread-bin /path/to/mgrep (from @mixedbread/mgrep)."
        )

    resolved = path.resolve()

    # Keep the historical Homebrew wrapper refusal (also covered by
    # is_skylakegrep_entry, but keep an explicit message for clarity).
    try:
        homebrew = Path("/opt/homebrew/bin/skygrep")
        if homebrew.exists() and resolved == homebrew.resolve():
            raise SystemExit(
                f"Refusing to use {resolved}: that path is the skylakegrep\n"
                "wrapper installed by this repo, not the Mixedbread CLI.\n"
                "Pass --mixedbread-bin pointing at a Mixedbread `mgrep` install."
            )
    except SystemExit:
        raise
    except OSError:
        pass

    if is_skylakegrep_entry(resolved):
        raise SystemExit(
            f"Refusing to use {resolved}: that path is this package's\n"
            "skygrep / skylakegrep CLI, not Mixedbread. Using it would\n"
            "silently compare skylakegrep against itself.\n"
            "Pass --mixedbread-bin pointing at Mixedbread `mgrep`\n"
            "(e.g. node_modules/.bin/mgrep from @mixedbread/mgrep)."
        )

    return str(resolved)


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
    mxbread_bin = resolve_mixedbread_bin(args.mixedbread_bin)

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
                "mgrep_local": {
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
    local_hits = sum(1 for r in rows if r["mgrep_local"]["hit"])

    return {
        "definition": {
            "benchmark_type": "Mixedbread cloud mgrep vs skylakegrep retrieval parity",
            "mixedbread_agent": "one Mixedbread `mgrep search` per task (cloud embeddings, paid quota)",
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
        description="Benchmark Mixedbread cloud mgrep vs skylakegrep on the same task set."
    )
    parser.add_argument("--root", default=".")
    parser.add_argument("--db-path")
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--chars-per-token", type=int, default=4)
    parser.add_argument(
        "--mixedbread-bin",
        help="Path to the Mixedbread `mgrep` binary (@mixedbread/mgrep). "
        "If omitted, uses `mgrep` from PATH when present. "
        "Never falls back to PATH `skygrep` (this package's entry); "
        "self-compare paths fail closed.",
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
