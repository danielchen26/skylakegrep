# SPDX-License-Identifier: Apache-2.0
"""Hard hit-rate comparison: --no-lazy (rg only) vs default (rg + lazy auto).

Runs the 10 Django oracle queries through the actual `skygrep search`
CLI (NOT the python API), once per config, on a fresh DB each time.
Records top-5 paths and checks against the expected / alternative
oracle answers. Prints a hit-rate comparison table so we can finally
say honestly: does the 0.5.x lazy auto-trigger improve hit-rate over
plain ripgrep cold-start, or not?

Usage:
    .venv/bin/python benchmarks/release-0.5.3-rg-vs-lazy.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORACLE = REPO_ROOT / "benchmarks" / "cross_repo" / "django.json"
DJANGO = Path("/tmp/oss-bench/django")

# Use the development venv's python so we exercise the in-tree code,
# not whatever happens to be on PyPI. The release verification step
# (separately) covers the PyPI install path.
PY = REPO_ROOT / ".venv" / "bin" / "python"


def parse_paths_from_output(stdout: str) -> list[str]:
    """Pull the top-K result paths out of stdout. Each result line
    looks like ``╭─ <path> ────  0.523`` (terminal renderer)."""
    paths = []
    for line in stdout.splitlines():
        m = re.match(r"^[╭│└][\s─]+(\S.*?)\s+─+\s+\d", line)
        if m:
            paths.append(m.group(1).strip())
    return paths


def hits(paths: list[str], expected: str, alts: list[str]) -> bool:
    """Suffix match: paths returned are absolute, expected is repo-relative."""
    for acc in [expected, *alts]:
        for p in paths:
            if p.endswith("/" + acc) or p.endswith(acc):
                return True
    return False


def run_one(query: str, db: str, no_lazy: bool) -> tuple[list[str], float, str]:
    cmd = [
        str(PY), "-m", "skylakegrep.src.cli",
        "search", "--auto-index",
        # Isolate the rg-vs-lazy comparison: turn off filename_shortcut
        # because for 7/10 of these queries, "migration", "auth",
        # "template", "form", etc. are filename-shaped tokens that
        # cause filename matches with score 1.0 to dominate the top-5,
        # which buries both rg AND lazy contributions. With this off,
        # the bench measures the actual value of the lazy tier.
        "--no-filename-shortcut",
    ]
    if no_lazy:
        cmd.append("--no-lazy")
    cmd.append(query)
    env = {**os.environ,
           "SKYGREP_DB_PATH": db,
           "PYTHONPATH": str(REPO_ROOT)}
    t0 = time.perf_counter()
    r = subprocess.run(cmd, cwd=str(DJANGO), env=env,
                       capture_output=True, text=True, timeout=120)
    elapsed = time.perf_counter() - t0
    paths = parse_paths_from_output(r.stdout)
    # Pull the telemetry footer so we can report "rg strong / lazy auto"
    foot = ""
    for line in r.stdout.splitlines():
        if line.startswith("[") and "·" in line and "cold-start" in line:
            foot = line.strip()
            break
    return paths, elapsed, foot


def main() -> int:
    if not DJANGO.exists():
        print(f"FATAL: clone Django to {DJANGO} first")
        return 2
    tasks = json.loads(ORACLE.read_text())
    print(f"=== Hard bench: {len(tasks)} queries × 2 configs on fresh Django DB ===\n")

    rows = []
    rg_only_hits = 0
    auto_hits = 0
    rg_only_total_t = 0.0
    auto_total_t = 0.0
    for i, t in enumerate(tasks, 1):
        q = t["question"]
        exp = t["expected"]
        alts = t.get("expected_alternatives", [])
        # Config A: --no-lazy (pure rg cold-start)
        db_a = f"/tmp/skg-bench53-norg-{i}.db"
        for ext in ("", ".lock", ".log"):
            try: Path(db_a + ext).unlink()
            except FileNotFoundError: pass
        paths_a, t_a, foot_a = run_one(q, db_a, no_lazy=True)
        h_a = hits(paths_a, exp, alts)
        rg_only_hits += int(h_a)
        rg_only_total_t += t_a

        # Config B: default (auto-trigger lazy)
        db_b = f"/tmp/skg-bench53-auto-{i}.db"
        for ext in ("", ".lock", ".log"):
            try: Path(db_b + ext).unlink()
            except FileNotFoundError: pass
        paths_b, t_b, foot_b = run_one(q, db_b, no_lazy=False)
        h_b = hits(paths_b, exp, alts)
        auto_hits += int(h_b)
        auto_total_t += t_b

        flag = "→" if h_a == h_b else ("✓ lazy rescued" if (h_b and not h_a) else "✗ lazy regressed")
        rows.append((t["id"], q[:55], h_a, h_b, t_a, t_b, flag))
        print(f"  Q{i:2d} {t['id']}: rg-only={'✓' if h_a else '✗'} "
              f"({t_a:5.2f}s)   auto={'✓' if h_b else '✗'} ({t_b:5.2f}s)   {flag}")
        print(f"      \"{q[:75]}\"")
        if not h_a and h_b:
            # Show what lazy added
            print(f"      auto top-5: {[Path(p).name for p in paths_b[:5]]}")

    n = len(tasks)
    print()
    print("=" * 78)
    print(f"  rg-only        : {rg_only_hits}/{n} ({100*rg_only_hits//n}%)  "
          f"total {rg_only_total_t:6.1f}s  avg {rg_only_total_t/n:.2f}s")
    print(f"  auto-trigger   : {auto_hits}/{n} ({100*auto_hits//n}%)  "
          f"total {auto_total_t:6.1f}s  avg {auto_total_t/n:.2f}s")
    print(f"  delta hit-rate : +{auto_hits - rg_only_hits} / {n}")
    print(f"  delta latency  : +{auto_total_t - rg_only_total_t:.1f}s "
          f"({(auto_total_t - rg_only_total_t)/n:.2f}s per query)")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
