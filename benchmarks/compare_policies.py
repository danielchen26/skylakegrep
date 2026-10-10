#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Paired policy comparison for a General Benchmark v2 report.

The universal benchmark's built-in comparison is hard-wired to
skygrep-first vs rg-only. This reads the full report (run without
--summary-only) and compares any policies it contains:

    python benchmarks/compare_policies.py report.json
    python benchmarks/compare_policies.py report.json --treatment skygrep-slim --baseline rg-agent

"Gate" is the benchmark's own sufficiency gate: the accepted path was
returned and every literal evidence term is present in the context.
Token figures are the report's tool-context tokens (whatever tokenizer the
report records). Each task's trials are reduced to their median first, so
repeated trials never inflate the task count.
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

POLICY_ORDER = ("skygrep-first", "skygrep-slim", "rg-agent", "rg-only")


def gate(row: dict) -> bool:
    return float(row.get("path_coverage", 0)) >= 1.0 and float(row.get("evidence_coverage", 0)) >= 1.0


def per_task(rows: list[dict]) -> dict[str, dict[tuple[str, str], dict]]:
    grouped: dict[str, dict[tuple[str, str], list[dict]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        grouped[row["policy"]][(row.get("repo", ""), row["task_id"])].append(row)
    out: dict[str, dict[tuple[str, str], dict]] = {}
    for policy, tasks in grouped.items():
        out[policy] = {
            key: {
                "tokens": st.median(r["context_tokens"] for r in trials),
                "calls": st.median(r["tool_calls"] for r in trials),
                "elapsed": st.median(r["elapsed_seconds"] for r in trials),
                "quality": st.mean(r["task_completion_quality"] for r in trials),
                "gate": all(gate(r) for r in trials),
                "stops": [r["stop_reason"] for r in trials],
            }
            for key, trials in tasks.items()
        }
    return out


def table(tasks: dict[str, dict[tuple[str, str], dict]]) -> str:
    lines = [
        f"{'policy':15}{'tasks':>6}{'gate':>7}{'quality':>9}{'median tok':>12}{'total tok':>13}{'median calls':>14}{'median s':>10}",
    ]
    for policy in [p for p in POLICY_ORDER if p in tasks] + sorted(set(tasks) - set(POLICY_ORDER)):
        values = list(tasks[policy].values())
        lines.append(
            f"{policy:15}{len(values):>6}{sum(v['gate'] for v in values):>7}"
            f"{st.mean(v['quality'] for v in values):>9.3f}"
            f"{st.median(v['tokens'] for v in values):>12,.0f}"
            f"{sum(v['tokens'] for v in values):>13,.0f}"
            f"{st.median(v['calls'] for v in values):>14g}"
            f"{st.median(v['elapsed'] for v in values):>10.2f}"
        )
    return "\n".join(lines)


def paired(tasks: dict, treatment: str, baseline: str) -> str:
    t, b = tasks.get(treatment, {}), tasks.get(baseline, {})
    keys = sorted(set(t) & set(b))
    if not keys:
        return f"no shared tasks between {treatment} and {baseline}"
    both = [k for k in keys if t[k]["gate"] and b[k]["gate"]]
    only_t = [k for k in keys if t[k]["gate"] and not b[k]["gate"]]
    only_b = [k for k in keys if b[k]["gate"] and not t[k]["gate"]]
    lines = [f"{treatment} vs {baseline}: {len(keys)} shared tasks"]
    lines.append(f"  gate passed by both: {len(both)}; only {treatment}: {len(only_t)}; only {baseline}: {len(only_b)}")
    if both:
        ratios = [b[k]["tokens"] / max(1.0, t[k]["tokens"]) for k in both]
        lines.append(
            f"  tokens on tasks both pass: median {baseline}/{treatment} = {st.median(ratios):.2f}x, "
            f"ratio of sums = {sum(b[k]['tokens'] for k in both) / max(1.0, sum(t[k]['tokens'] for k in both)):.2f}x, "
            f"{treatment} cheaper on {sum(r > 1 for r in ratios)}/{len(both)}"
        )
        lines.append(
            f"  median tokens: {treatment} {st.median(t[k]['tokens'] for k in both):,.0f} | "
            f"{baseline} {st.median(b[k]['tokens'] for k in both):,.0f}; "
            f"median calls {st.median(t[k]['calls'] for k in both):g} | {st.median(b[k]['calls'] for k in both):g}"
        )
    for label, group, pol in ((f"only {treatment}", only_t, treatment), (f"only {baseline}", only_b, baseline)):
        if group:
            lines.append(f"  {label}: " + ", ".join(f"{r}/{i}" for r, i in group))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", type=Path)
    parser.add_argument("--treatment", default=None)
    parser.add_argument("--baseline", default=None)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    rows = report.get("rows") or []
    if not rows:
        raise SystemExit("report has no rows; rerun the benchmark without --summary-only")
    tasks = per_task(rows)
    print(table(tasks))
    pairs = (
        [(args.treatment, args.baseline)]
        if args.treatment and args.baseline
        else [
            (a, b)
            for a, b in (
                ("skygrep-slim", "skygrep-first"),
                ("skygrep-slim", "rg-agent"),
                ("skygrep-first", "rg-agent"),
                ("skygrep-slim", "rg-only"),
            )
            if a in tasks and b in tasks
        ]
    )
    for treatment, baseline in pairs:
        print()
        print(paired(tasks, treatment, baseline))


if __name__ == "__main__":
    main()
