# SPDX-License-Identifier: Apache-2.0
"""Gate a full General Benchmark v2 run on measured indexing capacity.

The release-scale benchmark intentionally uses six complete public indexes.
Before paying for all six, this preflight measures a fresh Cobra index and
projects the other repositories from the exact files and chunks the current
indexer would process.  It is a capacity gate, not a performance claim: query
quality and efficiency are still decided only by the merged benchmark receipt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.public_fixtures import load_registry, prepare_repo, validate_repo_fixture
from skylakegrep.src.embeddings import MAX_INPUT_CHARS
from skylakegrep.src.indexer import collect_indexable_files, prepare_file_chunks


def repository_workload(root: Path) -> dict[str, int]:
    """Return the exact pre-embedding workload selected by the indexer."""

    files = collect_indexable_files(root)
    chunks = 0
    model_chars = 0
    for file in files:
        for chunk in prepare_file_chunks(file, root=root):
            chunks += 1
            model_chars += min(len(str(chunk["chunk"])), MAX_INPUT_CHARS)
    return {
        "files": len(files),
        "chunks": chunks,
        "model_chars": model_chars,
    }


def _cobra_reference(receipt: dict[str, Any], workload: dict[str, int]) -> dict[str, Any]:
    if receipt.get("schema_version") != 2:
        raise ValueError("Cobra receipt must use General Benchmark schema_version 2")
    sections = receipt.get("sections", [])
    if len(sections) != 1 or sections[0].get("repo") != "cobra":
        raise ValueError("capacity reference must contain exactly one completed Cobra section")
    environment = receipt.get("environment", {})
    generalization = receipt.get("generalization", {})
    source_gate = generalization.get("source_gate", {})
    if (
        environment.get("benchmark_source_tracked_clean") != "true"
        or not source_gate.get("passed")
    ):
        raise ValueError("capacity reference must come from a clean, source-gated benchmark tree")
    index = sections[0].get("index", {})
    if not index.get("refreshed") or not index.get("reset") or index.get("integrity") != "ok":
        raise ValueError("capacity reference must contain a fresh, reset, integrity-checked index")
    if int(index.get("chunks", 0)) != int(workload["chunks"]):
        raise ValueError("Cobra receipt chunk count does not match the current indexer workload")
    elapsed = float(index.get("elapsed_seconds", 0.0))
    if elapsed <= 0:
        raise ValueError("Cobra receipt must record positive fresh-index elapsed time")
    return {
        "repo": "cobra",
        "elapsed_seconds": elapsed,
        **workload,
    }


def capacity_report(
    workloads: dict[str, dict[str, int]],
    cobra_receipt: dict[str, Any],
    *,
    max_index_seconds: float,
) -> dict[str, Any]:
    """Project full-run capacity from a fresh Cobra reference receipt."""

    if set(workloads) != set(load_registry()):
        raise ValueError("capacity workload must cover the exact six-repository registry")
    if max_index_seconds <= 0:
        raise ValueError("max_index_seconds must be positive")
    cobra = _cobra_reference(cobra_receipt, workloads["cobra"])
    if cobra["chunks"] <= 0 or cobra["model_chars"] <= 0:
        raise ValueError("Cobra workload must contain non-empty model input")

    projections: dict[str, dict[str, Any]] = {}
    for repo, workload in sorted(workloads.items()):
        chunk_ratio = workload["chunks"] / cobra["chunks"]
        char_ratio = workload["model_chars"] / cobra["model_chars"]
        load_ratio = max(chunk_ratio, char_ratio)
        projected = cobra["elapsed_seconds"] * load_ratio
        projections[repo] = {
            **workload,
            "chunk_ratio_vs_cobra": round(chunk_ratio, 3),
            "model_char_ratio_vs_cobra": round(char_ratio, 3),
            "capacity_load_ratio": round(load_ratio, 3),
            "projected_index_seconds": round(projected, 3),
            "within_index_timeout": projected <= max_index_seconds,
        }

    limiting_repo = max(
        projections,
        key=lambda repo: projections[repo]["projected_index_seconds"],
    )
    passed = all(row["within_index_timeout"] for row in projections.values())
    environment = cobra_receipt["environment"]
    return {
        "schema_version": 1,
        "purpose": "runner capacity gate; not a retrieval-efficiency claim",
        "benchmark_source_commit": environment["benchmark_source_commit"],
        "benchmark_source_tracked_clean": True,
        "max_index_seconds": max_index_seconds,
        "projection_method": (
            "fresh Cobra elapsed multiplied by the larger of each repository's "
            "chunk-count ratio and capped model-input-character ratio"
        ),
        "reference": cobra,
        "repositories": projections,
        "capacity_gate": {
            "passed": passed,
            "limiting_repo": limiting_repo,
            "largest_projected_index_seconds": projections[limiting_repo][
                "projected_index_seconds"
            ],
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cobra-receipt", type=Path, required=True)
    parser.add_argument("--oss-root", type=Path, required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--clone-timeout", type=float, default=600.0)
    parser.add_argument("--max-index-seconds", type=float, default=18_000.0)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--require-capacity", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    registry = load_registry()
    workloads: dict[str, dict[str, int]] = {}
    for repo, spec in sorted(registry.items()):
        root = (
            prepare_repo(spec, args.oss_root, timeout=args.clone_timeout)
            if args.prepare
            else args.oss_root / spec.subdir
        )
        failures = validate_repo_fixture(root, spec) if root.is_dir() else ["repository missing"]
        if failures:
            raise RuntimeError(
                f"{repo}: public fixture validation failed: {'; '.join(failures[:10])}"
            )
        workloads[repo] = repository_workload(root)

    receipt = json.loads(args.cobra_receipt.read_text(encoding="utf-8"))
    report = capacity_report(
        workloads,
        receipt,
        max_index_seconds=args.max_index_seconds,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    if args.require_capacity and not report["capacity_gate"]["passed"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
