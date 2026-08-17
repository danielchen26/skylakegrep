"""Paired, quality-gated statistics for cross-repository benchmark rows."""

from __future__ import annotations

import random
import statistics
from collections import defaultdict
from typing import Any, Iterable, Optional


def _percentile(values: list[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def _rounded(value: Optional[float], digits: int = 3) -> Optional[float]:
    return None if value is None else round(value, digits)


def _distribution(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "p25": _rounded(_percentile(values, 0.25)),
        "median": _rounded(_percentile(values, 0.50)),
        "p75": _rounded(_percentile(values, 0.75)),
        "p95": _rounded(_percentile(values, 0.95)),
        "min": _rounded(min(values) if values else None),
        "max": _rounded(max(values) if values else None),
    }


def _hierarchical_bootstrap_median(
    values_by_repo_task: dict[str, dict[str, list[float]]],
    *,
    samples: int,
    seed: int,
) -> dict[str, Any]:
    repos = sorted(repo for repo, tasks in values_by_repo_task.items() if tasks)
    if not repos or samples <= 0:
        return {"samples": 0, "low": None, "high": None, "method": "not_available"}
    rng = random.Random(seed)
    medians: list[float] = []
    for _ in range(samples):
        sampled_values: list[float] = []
        for _repo_index in repos:
            repo = rng.choice(repos)
            tasks = values_by_repo_task[repo]
            task_ids = sorted(tasks)
            for _task_index in task_ids:
                task_id = rng.choice(task_ids)
                sampled_values.append(statistics.median(tasks[task_id]))
        medians.append(statistics.median(sampled_values))
    return {
        "samples": samples,
        "low": _rounded(_percentile(medians, 0.025)),
        "high": _rounded(_percentile(medians, 0.975)),
        "method": "hierarchical bootstrap over repos then unique tasks; task medians combine trials",
        "seed": seed,
    }


def _ratio(baseline: dict[str, Any], treatment: dict[str, Any], field: str) -> Optional[float]:
    numerator = float(baseline.get(field, 0.0))
    denominator = float(treatment.get(field, 0.0))
    if numerator < 0 or denominator <= 0:
        return None
    return numerator / denominator


def paired_efficiency(
    rows: Iterable[dict[str, Any]],
    *,
    treatment: str = "skygrep-first",
    baseline: str = "rg-only",
    quality_floor: float = 0.85,
    noninferiority_margin_pct: float = 2.0,
    bootstrap_samples: int = 2000,
    bootstrap_seed: int = 20260814,
    min_pairs: int = 30,
    min_repos: int = 3,
) -> dict[str, Any]:
    grouped: dict[tuple[str, str, str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        key = (
            str(row.get("repo", "unknown")),
            str(row.get("task_id", "unknown")),
            str(row.get("effort", "unknown")),
            int(row.get("trial", 1)),
        )
        grouped[key][str(row.get("policy", "unknown"))] = row

    pairs = [
        (key, policies[treatment], policies[baseline])
        for key, policies in sorted(grouped.items())
        if treatment in policies and baseline in policies
    ]
    treatment_quality = [float(pair[1].get("task_completion_quality", 0.0)) for pair in pairs]
    baseline_quality = [float(pair[2].get("task_completion_quality", 0.0)) for pair in pairs]
    treatment_completed = [1.0 if pair[1].get("work_completed") else 0.0 for pair in pairs]
    baseline_completed = [1.0 if pair[2].get("work_completed") else 0.0 for pair in pairs]

    mean_treatment_quality = statistics.fmean(treatment_quality) if treatment_quality else 0.0
    mean_baseline_quality = statistics.fmean(baseline_quality) if baseline_quality else 0.0
    treatment_completion = statistics.fmean(treatment_completed) if treatment_completed else 0.0
    baseline_completion = statistics.fmean(baseline_completed) if baseline_completed else 0.0
    margin = noninferiority_margin_pct / 100.0
    quality_gate = (
        bool(pairs)
        and mean_treatment_quality + margin >= mean_baseline_quality
        and treatment_completion + margin >= baseline_completion
    )

    eligible = [
        pair
        for pair in pairs
        if pair[1].get("work_completed")
        and pair[2].get("work_completed")
        and float(pair[1].get("task_completion_quality", 0.0)) >= quality_floor
        and float(pair[2].get("task_completion_quality", 0.0)) >= quality_floor
    ]

    fields = {
        "context_token_reduction_x": "context_tokens",
        "tool_call_reduction_x": "tool_calls",
        "measured_elapsed_ratio_x": "elapsed_seconds",
        "measured_tool_elapsed_ratio_x": "tool_elapsed_seconds",
    }
    distributions: dict[str, Any] = {}
    observation_distributions: dict[str, Any] = {}
    for output_name, field in fields.items():
        values_by_task: dict[tuple[str, str], list[float]] = defaultdict(list)
        for (repo, task, _effort, _trial), sky, rg in eligible:
            value = _ratio(rg, sky, field)
            if value is not None:
                values_by_task[(repo, task)].append(value)
        observation_values = [value for values in values_by_task.values() for value in values]
        task_values = [statistics.median(values) for values in values_by_task.values()]
        distributions[output_name] = _distribution(task_values)
        observation_distributions[output_name] = _distribution(observation_values)

    elapsed_delta_by_task: dict[tuple[str, str], list[float]] = defaultdict(list)
    for (repo, task, _effort, _trial), sky, rg in eligible:
        elapsed_delta_by_task[(repo, task)].append(
            float(sky.get("elapsed_seconds", 0.0))
            - float(rg.get("elapsed_seconds", 0.0))
        )
    elapsed_delta_observations = [
        value for values in elapsed_delta_by_task.values() for value in values
    ]
    elapsed_delta_tasks = [
        statistics.median(values) for values in elapsed_delta_by_task.values()
    ]
    distributions["measured_elapsed_delta_seconds"] = _distribution(elapsed_delta_tasks)
    observation_distributions["measured_elapsed_delta_seconds"] = _distribution(
        elapsed_delta_observations
    )

    context_by_repo_task: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    elapsed_ratio_by_repo_task: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    elapsed_delta_by_repo_task: dict[str, dict[str, list[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for (repo, task, _effort, _trial), sky, rg in eligible:
        value = _ratio(rg, sky, "context_tokens")
        if value is not None:
            context_by_repo_task[repo][task].append(value)
        elapsed_ratio = _ratio(rg, sky, "elapsed_seconds")
        if elapsed_ratio is not None:
            elapsed_ratio_by_repo_task[repo][task].append(elapsed_ratio)
        elapsed_delta_by_repo_task[repo][task].append(
            float(sky.get("elapsed_seconds", 0.0))
            - float(rg.get("elapsed_seconds", 0.0))
        )

    repo_results = {
        repo: {
            "paired_observations": sum(len(values) for values in tasks.values()),
            "tasks": len(tasks),
            "context_token_reduction_x": _distribution(
                [statistics.median(values) for values in tasks.values()]
            ),
            "measured_elapsed_ratio_x": _distribution(
                [
                    statistics.median(values)
                    for values in elapsed_ratio_by_repo_task[repo].values()
                ]
            ),
            "measured_elapsed_delta_seconds": _distribution(
                [
                    statistics.median(values)
                    for values in elapsed_delta_by_repo_task[repo].values()
                ]
            ),
        }
        for repo, tasks in sorted(context_by_repo_task.items())
    }
    ratio_of_sums = None
    if eligible:
        sky_tokens = sum(int(pair[1].get("context_tokens", 0)) for pair in eligible)
        rg_tokens = sum(int(pair[2].get("context_tokens", 0)) for pair in eligible)
        if sky_tokens > 0:
            ratio_of_sums = rg_tokens / sky_tokens

    eligible_fraction = len(eligible) / len(pairs) if pairs else 0.0
    eligible_tasks = sum(len(tasks) for tasks in context_by_repo_task.values())
    represented_repos = len(context_by_repo_task)
    sample_gate = eligible_tasks >= min_pairs and represented_repos >= min_repos
    claim_status = (
        "reportable"
        if quality_gate and eligible_fraction >= 0.8 and sample_gate
        else "insufficient"
    )
    return {
        "claim_status": claim_status,
        "claim_scope": "paired retrieval-workflow context; excludes model reasoning and provider billing tokens",
        "paired_rows": len(pairs),
        "quality_eligible_pairs": len(eligible),
        "quality_eligible_tasks": eligible_tasks,
        "quality_eligible_fraction": round(eligible_fraction, 3),
        "excluded_pairs": len(pairs) - len(eligible),
        "sample_gate": {
            "passed": sample_gate,
            "represented_repos": represented_repos,
            "minimum_repos": min_repos,
            "minimum_unique_tasks": min_pairs,
        },
        "quality_gate": {
            "passed": quality_gate,
            "quality_floor": quality_floor,
            "noninferiority_margin_pct": noninferiority_margin_pct,
            "treatment_work_quality_pct": round(mean_treatment_quality * 100.0, 1),
            "baseline_work_quality_pct": round(mean_baseline_quality * 100.0, 1),
            "work_quality_delta_pct": round((mean_treatment_quality - mean_baseline_quality) * 100.0, 1),
            "treatment_completion_pct": round(treatment_completion * 100.0, 1),
            "baseline_completion_pct": round(baseline_completion * 100.0, 1),
        },
        "paired_distributions": distributions,
        "paired_observation_distributions": observation_distributions,
        "context_token_ratio_of_sums": _rounded(ratio_of_sums),
        "context_token_median_95pct_ci": _hierarchical_bootstrap_median(
            context_by_repo_task,
            samples=bootstrap_samples,
            seed=bootstrap_seed,
        ),
        "by_repo": repo_results,
        "interpretation": (
            "Only quality-eligible paired tasks contribute to efficiency headlines. "
            "A lower-quality treatment cannot earn a reportable multiplier. "
            "Elapsed ratios are rg/skygrep; positive elapsed deltas mean skygrep is slower."
        ),
    }
