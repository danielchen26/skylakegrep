#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Public, reproducible cross-repo benchmark for skylakegrep.

Runs `benchmarks/parity_vs_ripgrep.py` against six pinned OSS
codebases, each with a checked-in public question and evidence set in
``benchmarks/public_tasks/``. Reports hit-rate and latency for
skygrep and a real ripgrep run side-by-side, so anyone can verify the
"skygrep is no worse than rg" floor and the "skygrep wins on
vocabulary mismatch" claim.

Reproduction
------------

    .venv/bin/python benchmarks/public_oss_bench.py --prepare --tokenizer tiktoken

The checked-in registry clones and checks out the exact six public commits;
moving branch tips are not accepted benchmark inputs.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmarks.public_fixtures import load_registry, prepare_repo, validate_repo_fixture

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OSS_ROOT = Path("/tmp/skygrep-general-v2-repos")
PUBLIC_REPOS = load_registry()


def run_one(label: str, fixture: Path, repo: Path, top_k: int, tokenizer: str) -> dict:
    """Run parity_vs_ripgrep on a single (fixture, repo) pair, return summary."""
    if not repo.exists():
        raise RuntimeError(f"{label}: repository is missing at {repo}")
    if not fixture.exists():
        raise RuntimeError(f"{label}: fixture is missing at {fixture}")

    print(f"\n=== {label} ===")
    print(f"  repo:    <oss-root>/{repo.name}")
    print(f"  tasks:   benchmarks/public_tasks/{fixture.name}")
    print(f"  top-k:   {top_k}")

    started = time.time()
    cmd = [
        sys.executable,
        str(REPO_ROOT / "benchmarks" / "parity_vs_ripgrep.py"),
        "--root", str(repo),
        "--tasks", str(fixture),
        "--top-k", str(top_k),
        "--tokenizer", tokenizer,
        "--summary-only",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ})
    elapsed = time.time() - started

    if proc.returncode != 0:
        raise RuntimeError(f"{label}: runner failed ({proc.returncode}): {proc.stderr.strip()[-400:]}")

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"{label}: could not parse JSON output: {e}") from e

    summary = result.get("summary", {})
    summary["_label"] = label
    summary["_wall_seconds"] = round(elapsed, 1)
    summary["_repo"] = f"<oss-root>/{repo.name}"
    return summary


def print_table(summaries: list[dict]) -> None:
    if not summaries:
        print("\nNo results.")
        return
    print("\n" + "=" * 88)
    print("PUBLIC OSS BENCHMARK · skygrep vs ripgrep · same fixture, same repo")
    print("=" * 88)
    header = (
        f"{'Fixture':<22} {'skygrep hit':>14} {'rg hit':>10} "
        f"{'skygrep s/q':>12} {'rg s/q':>10}"
    )
    print(header)
    print("-" * 88)
    sky_hits = rg_hits = total = 0
    for s in summaries:
        sky = s.get("skygrep_hit_rate", "?/?")
        rg = s.get("rg_hit_rate", "?/?")
        sky_lat = s.get("skygrep_avg_latency_seconds")
        rg_lat = s.get("rg_avg_latency_seconds")
        sky_lat_s = f"{sky_lat:.2f}" if sky_lat is not None else "—"
        rg_lat_s = f"{rg_lat:.2f}" if rg_lat is not None else "—"
        print(f"{s['_label']:<22} {sky:>14} {rg:>10} {sky_lat_s:>12} {rg_lat_s:>10}")
        try:
            num, denom = sky.split("/")
            sky_hits += int(num); total += int(denom)
            rg_hits += int(rg.split("/")[0])
        except Exception:
            pass
    print("-" * 88)
    if total:
        print(
            f"{'AGGREGATE':<22} {sky_hits}/{total} ({sky_hits/total:.0%})"
            + f"   {rg_hits}/{total} ({rg_hits/total:.0%})"
        )
    print("=" * 88)
    # Honest gate: skygrep must NOT be worse than rg on hit-rate
    print()
    if sky_hits >= rg_hits:
        print(f"✓ NO REGRESSION: skygrep ({sky_hits}/{total}) >= rg ({rg_hits}/{total}) on aggregate hit-rate.")
    else:
        print(f"⚠️  REGRESSION: skygrep ({sky_hits}/{total}) < rg ({rg_hits}/{total}) on aggregate hit-rate.")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--oss-root", type=Path, default=DEFAULT_OSS_ROOT,
                   help="Directory containing the cloned OSS repos. "
                        "Each repo is expected at <oss-root>/<name>.")
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--tokenizer", choices=["chars", "auto", "tiktoken"], default="chars")
    p.add_argument("--prepare", action="store_true", help="Clone and check out each exact public pin.")
    p.add_argument("--only", default=None,
                   help="Run only one public repository key.")
    args = p.parse_args()

    summaries: list[dict] = []
    for key, spec in PUBLIC_REPOS.items():
        if args.only and args.only != key:
            continue
        repo = (
            prepare_repo(spec, args.oss_root)
            if args.prepare
            else args.oss_root / spec.subdir
        )
        failures = validate_repo_fixture(repo, spec) if repo.is_dir() else ["repository missing"]
        if failures:
            raise RuntimeError(f"{key}: public fixture validation failed: {'; '.join(failures[:10])}")
        summaries.append(run_one(spec.label, spec.fixture, repo, args.top_k, args.tokenizer))

    print_table(summaries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
