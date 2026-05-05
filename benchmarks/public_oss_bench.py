#!/usr/bin/env python3
"""Public, reproducible cross-repo benchmark for skylakegrep.

Runs `benchmarks/parity_vs_ripgrep.py` against three pinned OSS
codebases (Django · React · Tokio), each with a hand-labeled question
set in ``benchmarks/cross_repo/``. Reports hit-rate and latency for
skygrep and a real ripgrep run side-by-side, so anyone can verify the
"skygrep is no worse than rg" floor and the "skygrep wins on
vocabulary mismatch" claim.

Reproduction
------------

    git clone --depth=1 https://github.com/django/django   /tmp/oss-bench/django
    git clone --depth=1 https://github.com/facebook/react  /tmp/oss-bench/react
    git clone --depth=1 https://github.com/tokio-rs/tokio  /tmp/oss-bench/tokio

    .venv/bin/python benchmarks/public_oss_bench.py
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OSS_ROOT = Path("/tmp/oss-bench")

FIXTURES: list[tuple[str, str, str]] = [
    # (label,           fixture file,                          oss subdir)
    ("Django (Python)", "benchmarks/cross_repo/django.json",   "django"),
    ("React (JS+TS)",   "benchmarks/cross_repo/react.json",    "react"),
    ("Tokio (Rust)",    "benchmarks/cross_repo/tokio.json",    "tokio"),
]


def run_one(label: str, fixture: Path, repo: Path, top_k: int) -> Optional[dict]:
    """Run parity_vs_ripgrep on a single (fixture, repo) pair, return summary."""
    if not repo.exists():
        print(f"  ⚠️  {label}: repo not found at {repo}, skipping")
        return None
    if not fixture.exists():
        print(f"  ⚠️  {label}: fixture {fixture} missing, skipping")
        return None

    print(f"\n=== {label} ===")
    print(f"  repo:    {repo}")
    print(f"  tasks:   {fixture}")
    print(f"  top-k:   {top_k}")

    started = time.time()
    cmd = [
        sys.executable,
        str(REPO_ROOT / "benchmarks" / "parity_vs_ripgrep.py"),
        "--root", str(repo),
        "--tasks", str(fixture),
        "--top-k", str(top_k),
        "--summary-only",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ})
    elapsed = time.time() - started

    if proc.returncode != 0:
        print(f"  ✗ runner failed (exit {proc.returncode})")
        print(f"  stderr (tail): {proc.stderr.strip()[-400:]}")
        return None

    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"  ✗ could not parse JSON output: {e}")
        print(f"  stdout (tail): {proc.stdout.strip()[-400:]}")
        return None

    summary = result.get("summary", {})
    summary["_label"] = label
    summary["_wall_seconds"] = round(elapsed, 1)
    summary["_repo"] = str(repo)
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
    p.add_argument("--only", default=None,
                   help="Run only one fixture (django|react|tokio).")
    args = p.parse_args()

    summaries: list[dict] = []
    for label, fixture_rel, oss_subdir in FIXTURES:
        if args.only and args.only != oss_subdir:
            continue
        fixture = REPO_ROOT / fixture_rel
        repo = args.oss_root / oss_subdir
        s = run_one(label, fixture, repo, args.top_k)
        if s is not None:
            summaries.append(s)

    print_table(summaries)
    return 0


if __name__ == "__main__":
    sys.exit(main())
