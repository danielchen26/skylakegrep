# SPDX-License-Identifier: Apache-2.0
"""End-to-end agent tool-context benchmark: skygrep vs raw ripgrep.

This benchmark measures the retrieval context an LLM-style coding agent would
receive before it continues reasoning. It does not call Claude, GPT, or any
remote model. Instead, it compares two local tool-use policies:

* ``skygrep-agent``: one structured ``skygrep --json --content`` call.
* ``rg-agent``: several raw ``rg`` term searches, then line-window context.

The score is intentionally context-centric: path coverage, evidence-term
coverage, token budget, tool-call budget, and latency. Those are the parts a
local tool can measure deterministically before a downstream LLM consumes the
context.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.agent_context_benchmark import extract_terms
from benchmarks.token_savings import approximate_tokens


DEFAULT_TASKS: list[dict[str, Any]] = [
    {
        "id": "locate-cli-entrypoint",
        "difficulty": "easy",
        "abstract_level": "locate",
        "query": "where is the command line entry point that registers skygrep subcommands?",
        "expected_paths": ["skylakegrep/src/cli.py"],
        "evidence_terms": ["click.group", "Commands", "search"],
        "include": ["skylakegrep/src/**"],
    },
    {
        "id": "locate-terminal-ui",
        "difficulty": "easy",
        "abstract_level": "locate",
        "query": "where is the terminal helix workflow rail rendered?",
        "expected_paths": ["skylakegrep/src/ui.py"],
        "evidence_terms": ["HELIX_ROLE_FRAMES", "helix_frame"],
        "include": ["skylakegrep/src/**"],
    },
    {
        "id": "snippet-agent-instructions",
        "difficulty": "medium",
        "abstract_level": "snippet",
        "query": "what instructions tell coding agents when to use content, detail full, answer, and json?",
        "expected_paths": ["skylakegrep/src/integrations.py"],
        "evidence_terms": ["SNIPPET_BODY", "--content", "--detail full", "--json"],
        "include": ["skylakegrep/src/integrations.py"],
    },
    {
        "id": "snippet-db-lock-status",
        "difficulty": "medium",
        "abstract_level": "snippet",
        "query": "how does the cold semantic path handle cross folder database locked failures?",
        "expected_paths": ["skylakegrep/src/cli.py", "tests/test_v0_4_ux.py"],
        "evidence_terms": ["database is locked", "background index is writing"],
        "include": ["skylakegrep/src/**", "tests/**"],
    },
    {
        "id": "deep-information-depth",
        "difficulty": "hard",
        "abstract_level": "deep",
        "query": "how does skygrep decide which information depth to show for agents and humans?",
        "expected_paths": [
            "README.md",
            "skylakegrep/src/cli.py",
            "skylakegrep/src/integrations.py",
        ],
        "evidence_terms": ["Information depth", "--detail full", "--answer", "--json"],
    },
    {
        "id": "deep-lazy-budget",
        "difficulty": "hard",
        "abstract_level": "deep",
        "query": "where are cold lazy semantic foreground budgets surfaced and tuned?",
        "expected_paths": ["README.md", "skylakegrep/src/cli.py"],
        "evidence_terms": [
            "SKYGREP_COLD_LAZY_TOTAL_BUDGET_S",
            "foreground budget",
            "background indexing",
        ],
    },
    {
        "id": "abstract-result-wrap",
        "difficulty": "hard",
        "abstract_level": "abstract",
        "query": "which code keeps terminal result cards from wrapping scores into the left workflow rail?",
        "expected_paths": ["skylakegrep/src/render.py", "tests/test_terminal_ui.py"],
        "evidence_terms": ["available_content_columns", "helix_result_header", "score"],
        "include": ["skylakegrep/src/**", "tests/**"],
    },
    {
        "id": "abstract-agent-json",
        "difficulty": "hard",
        "abstract_level": "abstract",
        "query": "which command should an LLM use to get machine readable search context instead of scraping terminal output?",
        "expected_paths": ["README.md", "skylakegrep/src/integrations.py", "docs/cli.html"],
        "evidence_terms": ["--json", "machine-readable", "do not scrape"],
    },
]


EFFORTS: dict[str, dict[str, Any]] = {
    "low": {
        "top": 3,
        "detail": "summary",
        "use_include": False,
        "rg_terms": 3,
        "rg_matches": 5,
        "rg_context": 1,
    },
    "medium": {
        "top": 5,
        "detail": "standard",
        "use_include": True,
        "rg_terms": 6,
        "rg_matches": 10,
        "rg_context": 2,
    },
    "high": {
        "top": 10,
        "detail": "standard",
        "use_include": True,
        "rg_terms": 10,
        "rg_matches": 20,
        "rg_context": 4,
    },
}


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _clean(text: str) -> str:
    return ANSI_RE.sub("", text)


def _lower_blob(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).lower()


def _path_hit_count(expected_paths: list[str], paths: list[str]) -> int:
    hits = 0
    for expected in expected_paths:
        if any(expected in path for path in paths):
            hits += 1
    return hits


def _hit_fraction(expected_paths: list[str], paths: list[str]) -> float:
    if not expected_paths:
        return 1.0
    return _path_hit_count(expected_paths, paths) / len(expected_paths)


def _path_precision(expected_paths: list[str], paths: list[str]) -> float:
    unique_paths = sorted({path for path in paths if path})
    if not unique_paths:
        return 0.0 if expected_paths else 1.0
    if not expected_paths:
        return 1.0
    matched_returned = 0
    for path in unique_paths:
        if any(expected in path for expected in expected_paths):
            matched_returned += 1
    return matched_returned / len(unique_paths)


def _missing_terms(terms: list[str], payload: str) -> list[str]:
    lowered = payload.lower()
    return [term for term in terms if term.lower() not in lowered]


def _term_fraction(terms: list[str], payload: str) -> float:
    if not terms:
        return 1.0
    return (len(terms) - len(_missing_terms(terms, payload))) / len(terms)


def _score_context(expected_paths: list[str], evidence_terms: list[str], paths: list[str], payload: str) -> dict[str, float]:
    path_coverage = _hit_fraction(expected_paths, paths)
    evidence_coverage = _term_fraction(evidence_terms, payload)
    path_precision = _path_precision(expected_paths, paths)
    return {
        "path_coverage": round(path_coverage, 3),
        "path_precision": round(path_precision, 3),
        "evidence_coverage": round(evidence_coverage, 3),
        "sufficiency": round((0.6 * path_coverage) + (0.4 * evidence_coverage), 3),
        "missing_paths": [
            expected
            for expected in expected_paths
            if not any(expected in path for path in paths)
        ],
        "missing_evidence_terms": _missing_terms(evidence_terms, payload),
    }


def _run(cmd: list[str], cwd: Path, timeout: float, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    merged_env.update(
        {
            "SKYGREP_NO_HINTS": "1",
            "SKYGREP_SETUP_AUTO_REFRESH": "0",
            "SKYGREP_UI_ANIMATION": "off",
            "SKYGREP_UI_ICONS": "off",
        }
    )
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        timeout=timeout,
        env=merged_env,
    )


def skygrep_context(root: Path, task: dict[str, Any], effort: dict[str, Any], timeout: float) -> dict[str, Any]:
    started = time.perf_counter()
    cmd = [
        sys.executable,
        "-m",
        "skylakegrep.src.cli",
        "search",
        "--json",
        "--content",
        "--detail",
        str(effort["detail"]),
        "--top",
        str(effort["top"]),
        "--no-auto-index",
    ]
    if effort["use_include"]:
        for pattern in task.get("include", []):
            cmd.extend(["--include", pattern])
    cmd.append(task["query"])
    proc = _run(cmd, root, timeout=timeout)
    elapsed = time.perf_counter() - started
    payload = _clean(proc.stdout)
    results: list[dict[str, Any]] = []
    if proc.returncode == 0 and payload.strip():
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, list):
                results = parsed
        except json.JSONDecodeError:
            results = []
    paths = [str(item.get("path", "")) for item in results if isinstance(item, dict)]
    return {
        "method": "skygrep",
        "tool_calls": 1,
        "latency_seconds": round(elapsed, 3),
        "context_tokens": approximate_tokens(payload or proc.stderr, 4),
        "paths": paths,
        "payload": payload,
        "stderr_tail": proc.stderr[-500:],
        "returncode": proc.returncode,
    }


def rg_context(root: Path, task: dict[str, Any], effort: dict[str, Any], timeout: float) -> dict[str, Any]:
    rg = shutil.which("rg")
    if not rg:
        raise RuntimeError("ripgrep is not on PATH")
    started = time.perf_counter()
    sections: list[str] = []
    paths: set[str] = set()
    terms = extract_terms(task["query"], max_terms=int(effort["rg_terms"]))
    globs = task.get("include", []) if effort["use_include"] else []
    for term in terms:
        cmd = [
            rg,
            "--json",
            "-i",
            "-F",
            "--max-count",
            str(effort["rg_matches"]),
            "-C",
            str(effort["rg_context"]),
        ]
        for pattern in globs:
            cmd.extend(["--glob", pattern])
        cmd.extend(["--", term, str(root)])
        try:
            proc = _run(cmd, root, timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode not in (0, 1):
            continue
        block: list[str] = [f"## rg term: {term}"]
        matched = False
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype not in {"match", "context"}:
                continue
            data = event.get("data", {})
            raw_path = data.get("path", {}).get("text", "")
            if not raw_path:
                continue
            try:
                rel = Path(raw_path).resolve().relative_to(root).as_posix()
            except ValueError:
                rel = raw_path
            line_no = data.get("line_number", "")
            line_text = data.get("lines", {}).get("text", "").rstrip()
            if etype == "match":
                paths.add(rel)
                matched = True
            marker = ":" if etype == "match" else "-"
            block.append(f"{rel}{marker}{line_no}{marker}{line_text}")
        if matched:
            sections.append("\n".join(block))
    payload = "\n\n".join(sections) if sections else "NO_MATCHES"
    elapsed = time.perf_counter() - started
    return {
        "method": "rg",
        "tool_calls": len(terms),
        "latency_seconds": round(elapsed, 3),
        "context_tokens": approximate_tokens(payload, 4),
        "paths": sorted(paths),
        "payload": payload,
        "returncode": 0,
    }


def _compact(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if k not in {"payload", "paths", "stderr_tail"}}


def _pct(value: float) -> float:
    return round(100.0 * value, 1)


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    n = len(rows)
    context_tokens = sum(int(r["context_tokens"]) for r in rows)
    sufficiency = sum(r["sufficiency"] for r in rows) / n
    return {
        "tasks": n,
        "path_accuracy_pct": _pct(sum(r["path_coverage"] for r in rows) / n),
        "path_precision_pct": _pct(sum(r["path_precision"] for r in rows) / n),
        "evidence_coverage_pct": _pct(sum(r["evidence_coverage"] for r in rows) / n),
        "sufficiency_pct": _pct(sufficiency),
        "tool_calls": sum(int(r["tool_calls"]) for r in rows),
        "total_latency_seconds": round(sum(float(r["latency_seconds"]) for r in rows), 3),
        "avg_latency_seconds": round(sum(float(r["latency_seconds"]) for r in rows) / n, 3),
        "context_tokens": context_tokens,
        "avg_context_tokens": round(context_tokens / n, 1),
        "sufficiency_per_1k_tokens": round((1000.0 * sufficiency) / max(1, context_tokens / n), 3),
        "failures": sum(1 for r in rows if int(r.get("returncode", 0)) != 0),
    }


def benchmark(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    if args.refresh_index:
        _run(
            [sys.executable, "-m", "skylakegrep.src.cli", "index", str(root), "--incremental"],
            root,
            timeout=args.index_timeout,
        )
    tasks = json.loads(Path(args.tasks).read_text()) if args.tasks else DEFAULT_TASKS
    selected_efforts = args.effort or list(EFFORTS)
    rows: list[dict[str, Any]] = []
    for effort_name in selected_efforts:
        effort = EFFORTS[effort_name]
        for task in tasks:
            for method, fn in (("skygrep", skygrep_context), ("rg", rg_context)):
                result = fn(root, task, effort, args.timeout)
                scores = _score_context(
                    task.get("expected_paths", []),
                    task.get("evidence_terms", []),
                    result["paths"],
                    _lower_blob(result["payload"]),
                )
                rows.append(
                    {
                        "method": method,
                        "effort": effort_name,
                        "task_id": task["id"],
                        "difficulty": task["difficulty"],
                        "abstract_level": task["abstract_level"],
                        "query": task["query"],
                        "expected_paths": task.get("expected_paths", []),
                        **scores,
                        **_compact(result),
                    }
                )

    by_method_effort: dict[str, dict[str, Any]] = {}
    for method in ("skygrep", "rg"):
        for effort_name in selected_efforts:
            key = f"{method}:{effort_name}"
            by_method_effort[key] = _aggregate(
                [r for r in rows if r["method"] == method and r["effort"] == effort_name]
            )

    by_level: dict[str, dict[str, Any]] = {}
    levels = sorted({r["abstract_level"] for r in rows})
    for level in levels:
        for method in ("skygrep", "rg"):
            key = f"{level}:{method}"
            by_level[key] = _aggregate(
                [r for r in rows if r["abstract_level"] == level and r["method"] == method]
            )

    totals = {
        "skygrep": _aggregate([r for r in rows if r["method"] == "skygrep"]),
        "rg": _aggregate([r for r in rows if r["method"] == "rg"]),
    }
    rg_total = totals["rg"]
    sky_total = totals["skygrep"]
    comparison = {
        "tool_call_reduction_x": round(rg_total["tool_calls"] / max(1, sky_total["tool_calls"]), 2),
        "context_token_reduction_x": round(rg_total["context_tokens"] / max(1, sky_total["context_tokens"]), 2),
        "latency_ratio_rg_over_skygrep": round(
            rg_total["total_latency_seconds"] / max(0.001, sky_total["total_latency_seconds"]),
            2,
        ),
        "sufficiency_delta_pct": round(sky_total["sufficiency_pct"] - rg_total["sufficiency_pct"], 1),
        "path_accuracy_delta_pct": round(sky_total["path_accuracy_pct"] - rg_total["path_accuracy_pct"], 1),
        "path_precision_delta_pct": round(sky_total["path_precision_pct"] - rg_total["path_precision_pct"], 1),
        "sufficiency_density_ratio_skygrep_over_rg": round(
            sky_total["sufficiency_per_1k_tokens"] / max(0.001, rg_total["sufficiency_per_1k_tokens"]),
            2,
        ),
    }
    return {
        "definition": {
            "benchmark": "agent tool-context depth benchmark",
            "skygrep_agent": "one structured skygrep --json --content call per task",
            "rg_agent": "multiple raw ripgrep term searches per task",
            "scores": "path coverage is 60% of sufficiency; evidence-term coverage is 40%; path precision and sufficiency density measure context noise",
            "privacy": "default tasks are generic repository-maintenance tasks; no local user folders or private filenames",
        },
        "parameters": {
            "root": str(root),
            "efforts": selected_efforts,
            "tasks": len(tasks),
            "timeout_seconds": args.timeout,
        },
        "totals": totals,
        "comparison": comparison,
        "by_method_effort": by_method_effort,
        "by_abstract_level": by_level,
        "rows": rows if not args.summary_only else [_compact(r) for r in rows],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark skygrep agent context vs raw rg agent context.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--tasks", help="Optional JSON task fixture")
    parser.add_argument("--effort", choices=sorted(EFFORTS), action="append", help="Run one effort profile; repeatable")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--refresh-index", action="store_true", help="Refresh skygrep index once before timing tasks")
    parser.add_argument("--index-timeout", type=float, default=180.0)
    parser.add_argument("--summary-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    print(json.dumps(benchmark(parse_args()), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
