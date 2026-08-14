"""Fast, deterministic agent-retrieval contract benchmark for CI.

The release-scale benchmark uses real repositories and a local Ollama model,
which is intentionally too expensive for every pull request. This fixture
exercises the real hybrid candidate-recall implementation without a model:
path, bounded ripgrep, SQLite chunks, source priors, and evidence summaries.
Its JSON matches ``closed_loop_regression_gate.py`` so CI enforces both task
quality and context economy instead of merely unit-testing the gate parser.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from skylakegrep.src.candidate_recall import build_agent_context_results
from skylakegrep.src.cli import render_json_results
from skylakegrep.src.hybrid import extract_query_terms
from skylakegrep.src.storage import init_db, store_chunks_batch


TASKS = (
    {
        "query": "where is access token refresh implemented?",
        "expected": "src/session.py",
        "evidence": ("refresh_access_token", "expires_at"),
    },
    {
        "query": "how does the daemon reuse hybrid agent context evidence?",
        "expected": "src/daemon.py",
        "evidence": ("run_agent_context_search", "candidate_recall_lanes"),
    },
    {
        "query": "where is strict verification checking index freshness?",
        "expected": "src/verification.py",
        "evidence": ("strict_verification", "file_mtime"),
    },
    {
        "query": "which code routes natural language search but leaves regex to rg?",
        "expected": "src/routing.py",
        "evidence": ("classify_query", "exact_regex"),
    },
    {
        "query": "which test proves direct and daemon results have parity?",
        "expected": "tests/test_parity.py",
        "evidence": ("same_result_paths", "agent_summary"),
    },
    {
        "query": "what policy tells agents to follow up uncertain evidence?",
        "expected": "docs/agent-policy.md",
        "evidence": ("quality=uncertain", "suggested_followup_probe"),
    },
)


FILES = {
    "src/session.py": (
        "def refresh_access_token(expires_at):\n"
        "    return issue_replacement_token(expires_at)\n"
    ),
    "src/daemon.py": (
        "def daemon_agent_context(query):\n"
        "    results = run_agent_context_search(query)\n"
        "    return results, candidate_recall_lanes(results)\n"
    ),
    "src/verification.py": (
        "def strict_verification(indexed, source):\n"
        "    return indexed.file_mtime == source.stat().st_mtime\n"
    ),
    "src/routing.py": (
        "def classify_query(text):\n"
        "    return exact_regex(text) if is_regex(text) else 'natural-language'\n"
    ),
    "tests/test_parity.py": (
        "def test_direct_daemon_parity():\n"
        "    assert same_result_paths(direct, daemon)\n"
        "    assert direct.agent_summary == daemon.agent_summary\n"
    ),
    "docs/agent-policy.md": (
        "When quality=uncertain, run the suggested_followup_probe before making a claim.\n"
    ),
}


def _tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def _write_fixture(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    fixture_files = dict(FILES)
    noise_fragments = (
        "access token reference notes",
        "refresh operations overview",
        "daemon lifecycle notes",
        "agent context examples",
        "strict policy discussion",
        "index freshness observations",
        "natural language examples",
        "regex syntax notes",
        "direct daemon comparison",
        "result parity observations",
        "uncertain evidence policy",
        "follow up agent notes",
    )
    for index in range(48):
        fragment = noise_fragments[index % len(noise_fragments)]
        fixture_files[f"notes/noise_{index:02d}.md"] = (fragment + "\n") * 20
    for index, (relative, content) in enumerate(fixture_files.items()):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        rows.append(
            {
                "file": str(path),
                "chunk": f"[file: {relative}]\n{content}",
                "language": "python" if path.suffix == ".py" else "markdown",
                "chunk_index": index,
                "file_mtime": path.stat().st_mtime,
                "start_line": 1,
                "end_line": len(content.splitlines()),
                "start_byte": 0,
                "end_byte": len(content.encode("utf-8")),
                "embedding": [1.0, 0.0],
            }
        )
    return rows


def _raw_rg_context(root: Path, query: str) -> tuple[str, int, float]:
    started = time.perf_counter()
    sections: list[str] = []
    path_term_hits: dict[Path, int] = {}
    calls = 0
    for term in extract_query_terms(query, max_terms=8):
        calls += 1
        proc = subprocess.run(
            ["rg", "-n", "-i", "--fixed-strings", "--", term, str(root)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.stdout:
            sections.append(proc.stdout)
            term_paths: set[Path] = set()
            for line in proc.stdout.splitlines():
                raw = line.split(":", 1)[0]
                path = Path(raw)
                term_paths.add(path)
            for path in term_paths:
                path_term_hits[path] = path_term_hits.get(path, 0) + 1
    # Ripgrep traversal order can vary across filesystems.  Rank by the number
    # of distinct query terms found in each file, then by path, so the modeled
    # agent reads stable, relevant candidates without oracle knowledge.
    ranked_paths = sorted(
        path_term_hits,
        key=lambda item: (-path_term_hits[item], str(item)),
    )
    for path in ranked_paths[:12]:
        calls += 1
        try:
            sections.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n".join(sections), calls, time.perf_counter() - started


def _task_score(expected: str, evidence: tuple[str, ...], payload: str, paths: list[str]):
    path_coverage = float(any(path.endswith(expected) for path in paths))
    lowered = payload.lower()
    evidence_coverage = sum(term.lower() in lowered for term in evidence) / len(evidence)
    sufficiency = (0.6 * path_coverage) + (0.4 * evidence_coverage)
    return path_coverage, evidence_coverage, sufficiency


def run_benchmark() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        conn = init_db(root / "index.db")
        store_chunks_batch(conn, _write_fixture(root))
        sky_rows = []
        rg_rows = []
        for task in TASKS:
            started = time.perf_counter()
            results, _ = build_agent_context_results(
                conn,
                task["query"],
                root,
                top_k=5,
            )
            sky_elapsed = time.perf_counter() - started
            sky_payload = render_json_results(results, include_snippet=True)
            sky_paths = [str(result.get("path") or "") for result in results]
            sky_score = _task_score(
                task["expected"], task["evidence"], sky_payload, sky_paths
            )
            sky_rows.append(
                {
                    "scores": sky_score,
                    "tokens": _tokens(sky_payload),
                    "elapsed": sky_elapsed,
                    "tool_calls": 1,
                }
            )

            rg_payload, rg_calls, rg_elapsed = _raw_rg_context(root, task["query"])
            rg_paths = [line.split(":", 1)[0] for line in rg_payload.splitlines() if ":" in line]
            rg_score = _task_score(
                task["expected"], task["evidence"], rg_payload, rg_paths
            )
            rg_rows.append(
                {
                    "scores": rg_score,
                    "tokens": _tokens(rg_payload),
                    "elapsed": rg_elapsed,
                    "tool_calls": rg_calls,
                }
            )
        conn.close()

    def totals(rows):
        count = len(rows)
        path = sum(row["scores"][0] for row in rows) / count
        evidence = sum(row["scores"][1] for row in rows) / count
        sufficiency = sum(row["scores"][2] for row in rows) / count
        tokens = sum(row["tokens"] for row in rows)
        elapsed = sum(row["elapsed"] for row in rows)
        estimated = elapsed + (tokens / 50.0)
        quality = (path + evidence + sufficiency) / 3.0
        return {
            "path_coverage_pct": round(path * 100, 1),
            "evidence_coverage_pct": round(evidence * 100, 1),
            "sufficiency_pct": round(sufficiency * 100, 1),
            "completed_tasks": sum(
                row["scores"][0] == 1.0 and row["scores"][1] == 1.0
                for row in rows
            ),
            "context_tokens": tokens,
            "tool_calls": sum(row["tool_calls"] for row in rows),
            "retrieval_elapsed_seconds": round(elapsed, 4),
            "estimated_agent_elapsed_seconds": round(estimated, 4),
            "work_quality_per_minute": round(quality * 60.0 / max(estimated, 0.001), 4),
        }

    sky = totals(sky_rows)
    rg = totals(rg_rows)
    comparison = {
        "context_token_reduction_x": round(
            rg["context_tokens"] / max(sky["context_tokens"], 1), 2
        ),
        "estimated_agent_elapsed_ratio_rg_over_skygrep": round(
            rg["estimated_agent_elapsed_seconds"]
            / max(sky["estimated_agent_elapsed_seconds"], 0.001),
            2,
        ),
        "work_quality_per_minute_ratio_skygrep_over_rg": round(
            sky["work_quality_per_minute"]
            / max(rg["work_quality_per_minute"], 0.0001),
            2,
        ),
    }
    return {
        "benchmark": "ci-agent-contract",
        "tasks": len(TASKS),
        "aggregate": {
            "totals": {"skygrep-first": sky, "rg-only": rg},
            "comparison": comparison,
        },
    }


def main() -> None:
    print(json.dumps(run_benchmark(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
