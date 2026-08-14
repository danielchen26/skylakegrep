"""Closed-loop agent benchmark: skygrep-first vs raw-rg-only.

This benchmark models the *loop* an LLM coding agent actually runs:

1. call a search tool,
2. inspect whether the returned context is sufficient,
3. escalate depth or fallback when it is not,
4. stop when enough evidence has been collected or the policy budget is spent.

It does not call a remote LLM. The scorer is a deterministic evaluator with
generic repository-maintenance tasks. That keeps the benchmark reproducible
while still measuring the dimensions that dominate agent cost: elapsed tool
time, number of tool calls, context tokens handed to the next reasoning turn,
path coverage, evidence coverage, sufficiency, and a deterministic proxy for
final task-completion quality.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.agent_context_benchmark import extract_terms
from benchmarks.agent_tool_depth_benchmark import DEFAULT_TASKS, EFFORTS, _score_context
from benchmarks.token_savings import approximate_tokens


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PROBE_STOPWORDS = {
    "a",
    "an",
    "about",
    "after",
    "against",
    "and",
    "are",
    "as",
    "at",
    "before",
    "between",
    "but",
    "by",
    "can",
    "could",
    "does",
    "for",
    "from",
    "has",
    "have",
    "handle",
    "how",
    "if",
    "incom",
    "incoming",
    "in",
    "is",
    "it",
    "into",
    "of",
    "on",
    "or",
    "should",
    "that",
    "the",
    "this",
    "to",
    "turn",
    "turned",
    "turning",
    "turns",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
}

SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".m",
    ".mm",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scala",
    ".swift",
    ".ts",
    ".tsx",
}

LOW_VALUE_PATH_PARTS = (
    "/.git/",
    "/bench/",
    "/benchmark/",
    "/benchmarks/",
    "/docs/",
    "/doc/",
    "/examples/",
    "/fixtures/",
    "/locale/",
    "/locales/",
    "/node_modules/",
    "/scripts/",
    "/test/",
    "/tests/",
    "/vendor/",
)


COMPLETION_CONTRACTS: dict[str, dict[str, Any]] = {
    "locate-cli-entrypoint": {
        "deliverable": "path_decision",
        "quality_terms": [],
        "min_paths": 1,
    },
    "locate-terminal-ui": {
        "deliverable": "path_decision",
        "quality_terms": [],
        "min_paths": 1,
    },
    "snippet-agent-instructions": {
        "deliverable": "source_evidence",
        "quality_terms": ["SNIPPET_BODY", "--content", "--detail full", "--json"],
        "min_paths": 1,
    },
    "snippet-db-lock-status": {
        "deliverable": "source_evidence",
        "quality_terms": ["database is locked", "background index is writing"],
        "min_paths": 2,
    },
    "deep-information-depth": {
        "deliverable": "multi_file_explanation",
        "quality_terms": ["Information depth", "--content", "--detail full", "--answer", "--json"],
        "min_paths": 3,
    },
    "deep-lazy-budget": {
        "deliverable": "multi_file_explanation",
        "quality_terms": [
            "SKYGREP_COLD_LAZY_TOTAL_BUDGET_S",
            "foreground budget",
            "background indexing",
        ],
        "min_paths": 2,
    },
    "abstract-result-wrap": {
        "deliverable": "architecture_mapping",
        # Require the score-rendering concept, not one arbitrary numeric value
        # from a test fixture. Fixture literals are not architectural evidence.
        "quality_terms": ["available_content_columns", "helix_result_header", "score"],
        "min_paths": 2,
    },
    "abstract-agent-json": {
        "deliverable": "architecture_mapping",
        "quality_terms": ["--json", "machine-readable", "do not scrape"],
        "min_paths": 3,
    },
}


def _clean(text: str) -> str:
    return ANSI_RE.sub("", text)


def _lower_payload(payloads: list[str]) -> str:
    return "\n\n".join(payloads).lower()


def _run(
    cmd: list[str],
    cwd: Path,
    timeout: float,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
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


def _safe_rel(root: Path, raw_path: str) -> str:
    try:
        return Path(raw_path).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return raw_path


def _dedupe(seq: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _probe_terms(query: str, max_terms: int) -> list[str]:
    """Terms for path probes.

    The generic extractor intentionally filters aggressively for semantic
    prompts, but path probes need to preserve short domain tokens such as URL,
    ORM, SQL, IO, or JSX because those often map directly to source paths.
    """

    raw_terms: list[str] = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_+-]*", query):
        lowered = token.lower().strip("_+-")
        if len(lowered) < 2 or lowered in PROBE_STOPWORDS:
            continue
        raw_terms.append(lowered)
        if len(lowered) > 3 and lowered.endswith("s"):
            raw_terms.append(lowered[:-1])
    semantic_terms = [
        term.lower()
        for term in extract_terms(query, max_terms=max_terms)
        if term.lower() not in PROBE_STOPWORDS
    ]
    return _dedupe([*raw_terms, *semantic_terms])[:max_terms]


def _root_noise_terms(root: Path) -> set[str]:
    """Terms that usually identify the repository, not the searched concept."""

    name = root.name.lower()
    pieces = re.split(r"[-_.]+", name)
    return {name, *[piece for piece in pieces if len(piece) > 2]}


def _selected_probe_terms(root: Path, query: str, max_terms: int) -> list[str]:
    terms = _probe_terms(query, max_terms=max_terms)
    noise = _root_noise_terms(root)
    filtered = [term for term in terms if term not in noise]
    return filtered or terms


def _path_probe_rank(path: str, matched_terms: set[str]) -> tuple[float, ...]:
    """Rank path-probe candidates without project-specific keywords.

    The probe is meant to rescue a missing anchor before content-heavy fallback.
    It should therefore prefer files that match multiple independent query
    terms, look like source implementation files, and avoid broad docs/tests
    matches unless they clearly win on term evidence.
    """

    lower = f"/{path.lower()}"
    suffix = Path(path).suffix.lower()
    term_hits = len(matched_terms)
    term_in_path = sum(1 for term in matched_terms if term and term in lower)
    source_bonus = 1 if suffix in SOURCE_SUFFIXES else 0
    low_value_penalty = sum(1 for part in LOW_VALUE_PATH_PARTS if part in lower)
    generated_penalty = 1 if "generated" in lower or "snapshot" in lower else 0
    return (
        -term_hits,
        -source_bonus,
        low_value_penalty + generated_penalty,
        -term_in_path,
        len(path),
    )


@dataclass
class StepResult:
    name: str
    tool_calls: int
    elapsed_seconds: float
    context_tokens: int
    paths: list[str] = field(default_factory=list)
    payload: str = ""
    returncode: int = 0
    error: str = ""

    def compact(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "context_tokens": self.context_tokens,
            "paths": len(self.paths),
            "returncode": self.returncode,
            **({"error": self.error[-180:]} if self.error else {}),
        }


def _skygrep_step(
    root: Path,
    query: str,
    *,
    timeout: float,
    top: int,
    detail: str,
    content: bool = True,
    includes: list[str] | None = None,
    rerank: bool = False,
    name: str,
) -> StepResult:
    started = time.perf_counter()
    cmd = [
        sys.executable,
        "-m",
        "skylakegrep.src.cli",
        "search",
        "--json",
        "--top",
        str(top),
        "--no-auto-index",
    ]
    if not rerank:
        cmd.append("--no-rerank")
    if not content:
        cmd.append("--no-content")
    if content:
        cmd.extend(["--content", "--detail", detail])
    for pattern in includes or []:
        cmd.extend(["--include", pattern])
    cmd.append(query)
    try:
        proc = _run(cmd, root, timeout=timeout)
        elapsed = time.perf_counter() - started
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - started
        return StepResult(
            name=name,
            tool_calls=1,
            elapsed_seconds=elapsed,
            context_tokens=0,
            returncode=124,
            error=f"timeout after {exc.timeout}s",
        )
    payload = _clean(proc.stdout)
    paths: list[str] = []
    try:
        parsed = json.loads(payload) if payload.strip() else []
    except json.JSONDecodeError:
        parsed = []
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict) and item.get("path"):
                paths.append(str(item["path"]))
    return StepResult(
        name=name,
        tool_calls=1,
        elapsed_seconds=elapsed,
        context_tokens=approximate_tokens(payload or proc.stderr, 4),
        paths=_dedupe(paths),
        payload=payload or proc.stderr,
        returncode=proc.returncode,
        error=proc.stderr[-500:],
    )


def _rg_step(
    root: Path,
    query: str,
    *,
    timeout: float,
    terms: int,
    max_matches: int,
    context: int,
    includes: list[str] | None = None,
    name: str,
) -> StepResult:
    rg = shutil.which("rg")
    if not rg:
        return StepResult(
            name=name,
            tool_calls=0,
            elapsed_seconds=0.0,
            context_tokens=0,
            returncode=127,
            error="ripgrep is not on PATH",
        )
    started = time.perf_counter()
    sections: list[str] = []
    paths: list[str] = []
    selected_terms = _selected_probe_terms(root, query, max_terms=terms)
    for term in selected_terms:
        cmd = [
            rg,
            "--json",
            "-i",
            "-F",
            "--max-count",
            str(max_matches),
            "-C",
            str(context),
        ]
        for pattern in includes or []:
            cmd.extend(["--glob", pattern])
        cmd.extend(["--", term, str(root)])
        try:
            proc = _run(cmd, root, timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode not in (0, 1):
            continue
        block = [f"## rg term: {term}"]
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
            rel = _safe_rel(root, raw_path)
            line_no = data.get("line_number", "")
            line_text = data.get("lines", {}).get("text", "").rstrip()
            if etype == "match":
                paths.append(rel)
                matched = True
            marker = ":" if etype == "match" else "-"
            block.append(f"{rel}{marker}{line_no}{marker}{line_text}")
        if matched:
            sections.append("\n".join(block))
    payload = "\n\n".join(sections) if sections else "NO_MATCHES"
    elapsed = time.perf_counter() - started
    return StepResult(
        name=name,
        tool_calls=len(selected_terms),
        elapsed_seconds=elapsed,
        context_tokens=approximate_tokens(payload, 4),
        paths=_dedupe(paths),
        payload=payload,
        returncode=0,
    )


def _rg_path_probe_step(
    root: Path,
    query: str,
    *,
    timeout: float,
    terms: int,
    max_paths: int,
    includes: list[str] | None = None,
    name: str,
) -> StepResult:
    """Use rg as a path-only probe before paying for content context."""

    rg = shutil.which("rg")
    if not rg:
        return StepResult(
            name=name,
            tool_calls=0,
            elapsed_seconds=0.0,
            context_tokens=0,
            returncode=127,
            error="ripgrep is not on PATH",
        )
    started = time.perf_counter()
    root = root.resolve()
    hits_by_path: dict[str, set[str]] = {}
    selected_terms = _selected_probe_terms(root, query, max_terms=terms)
    per_term_limit = max(250, max_paths * 80)
    for term in selected_terms:
        cmd = [
            rg,
            "-i",
            "-F",
            "-l",
            "--max-count",
            "1",
        ]
        for pattern in includes or []:
            cmd.extend(["--glob", pattern])
        cmd.extend(["--", term, str(root)])
        try:
            proc = _run(cmd, root, timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
        if proc.returncode not in (0, 1):
            continue
        for line in proc.stdout.splitlines()[:per_term_limit]:
            rel = _safe_rel(root, line.strip())
            if not rel:
                continue
            hits_by_path.setdefault(rel, set()).add(term.lower())
    ranked_paths = sorted(
        hits_by_path,
        key=lambda path: (*_path_probe_rank(path, hits_by_path[path]), path),
    )[:max_paths]
    sections = [
        f"{path}\tterms={','.join(sorted(hits_by_path[path]))}"
        for path in ranked_paths
    ]
    payload = "\n".join(sections) if sections else "NO_PATH_PROBE_MATCHES"
    elapsed = time.perf_counter() - started
    return StepResult(
        name=name,
        tool_calls=len(selected_terms),
        elapsed_seconds=elapsed,
        context_tokens=approximate_tokens(payload, 4),
        paths=ranked_paths,
        payload=payload,
        returncode=0,
    )


def _read_paths_step(
    root: Path,
    paths: list[str],
    *,
    max_files: int,
    max_chars: int,
    name: str,
) -> StepResult:
    started = time.perf_counter()
    sections: list[str] = []
    read_paths: list[str] = []
    for rel in _dedupe(paths)[:max_files]:
        target = (root / rel).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if not target.is_file():
            continue
        try:
            text = target.read_text(errors="ignore")[:max_chars]
        except OSError:
            continue
        sections.append(f"## read file: {rel}\n{text}")
        read_paths.append(rel)
    payload = "\n\n".join(sections) if sections else "NO_READS"
    elapsed = time.perf_counter() - started
    return StepResult(
        name=name,
        tool_calls=len(read_paths),
        elapsed_seconds=elapsed,
        context_tokens=approximate_tokens(payload, 4),
        paths=read_paths,
        payload=payload,
        returncode=0,
    )


def _current_score(task: dict[str, Any], payloads: list[str], paths: list[str]) -> dict[str, Any]:
    return _score_context_for_task(task, payloads, _dedupe(paths))


def _term_fraction(terms: list[str], payload: str) -> float:
    if not terms:
        return 1.0
    lowered = payload.lower()
    hits = sum(1 for term in terms if term.lower() in lowered)
    return hits / len(terms)


def _path_fraction(expected_paths: list[str], paths: list[str]) -> float:
    if not expected_paths:
        return 1.0
    found = 0
    for expected in expected_paths:
        if any(expected in path for path in paths):
            found += 1
    return found / len(expected_paths)


def _path_group_fraction(expected_groups: list[list[str]], paths: list[str]) -> float:
    if not expected_groups:
        return 1.0
    found = 0
    for group in expected_groups:
        if any(candidate and any(candidate in path for path in paths) for candidate in group):
            found += 1
    return found / len(expected_groups)


def _path_group_precision(expected_groups: list[list[str]], paths: list[str]) -> float:
    unique_paths = sorted({path for path in paths if path})
    if not unique_paths:
        return 0.0 if expected_groups else 1.0
    matched = 0
    for path in unique_paths:
        if any(candidate and candidate in path for group in expected_groups for candidate in group):
            matched += 1
    return matched / len(unique_paths)


def _missing_path_groups(expected_groups: list[list[str]], paths: list[str]) -> list[str]:
    missing: list[str] = []
    for group in expected_groups:
        if not any(candidate and any(candidate in path for path in paths) for candidate in group):
            missing.append(" OR ".join(group))
    return missing


def _score_context_for_task(task: dict[str, Any], payloads: list[str], paths: list[str]) -> dict[str, Any]:
    expected_groups = task.get("expected_path_groups")
    payload = _lower_payload(payloads)
    contract = COMPLETION_CONTRACTS.get(task.get("id", ""), {})
    deliverable = contract.get(
        "deliverable",
        task.get("deliverable", task.get("abstract_level", "generic")),
    )
    # A locate task asks for a path decision. Requiring body-level symbols in
    # its stopping score contradicts the path-only agent-fast contract and
    # forces needless file reads after the correct path is already known.
    evidence_terms = [] if deliverable == "path_decision" else task.get("evidence_terms", [])
    if expected_groups:
        path_coverage = _path_group_fraction(expected_groups, paths)
        evidence_coverage = _term_fraction(evidence_terms, payload)
        path_precision = _path_group_precision(expected_groups, paths)
        return {
            "path_coverage": round(path_coverage, 3),
            "path_precision": round(path_precision, 3),
            "evidence_coverage": round(evidence_coverage, 3),
            "sufficiency": round((0.6 * path_coverage) + (0.4 * evidence_coverage), 3),
            "missing_paths": _missing_path_groups(expected_groups, paths),
            "missing_evidence_terms": [
                term for term in evidence_terms if term.lower() not in payload
            ],
        }
    return _score_context(
        task.get("expected_paths", []),
        evidence_terms,
        _dedupe(paths),
        payload,
    )


def _completion_quality(
    task: dict[str, Any],
    final_score: dict[str, Any],
    payloads: list[str],
    paths: list[str],
    *,
    sufficient_threshold: float,
) -> dict[str, Any]:
    """Estimate whether the retrieved context can produce a correct final answer.

    This is deliberately stricter than retrieval sufficiency. It asks whether a
    downstream agent has enough path decisions, source facts, and low-noise
    context to finish the task without guessing. The metric is deterministic so
    releases can compare policy changes without a stochastic judge model.
    """

    contract = COMPLETION_CONTRACTS.get(task["id"], {})
    deliverable = contract.get("deliverable", task.get("deliverable", task.get("abstract_level", "generic")))
    unique_paths = _dedupe(paths)
    payload = _lower_payload(payloads)
    expected_paths = task.get("expected_paths", [])
    expected_groups = task.get("expected_path_groups")
    quality_terms = list(
        contract["quality_terms"]
        if "quality_terms" in contract
        else task.get("quality_terms") or task.get("evidence_terms", [])
    )
    required_path_count = int(contract.get("min_paths", task.get("min_paths", max(1, len(expected_groups or expected_paths)))))

    path_score = (
        _path_group_fraction(expected_groups, unique_paths)
        if expected_groups
        else _path_fraction(expected_paths, unique_paths)
    )
    fact_score = _term_fraction(quality_terms, payload)
    precision_score = float(final_score.get("path_precision", 0.0))
    if expected_groups:
        enough_paths = min(
            1.0,
            sum(
                1
                for group in expected_groups
                if any(candidate and any(candidate in path for path in unique_paths) for candidate in group)
            )
            / max(1, required_path_count),
        )
    else:
        enough_paths = min(1.0, len([p for p in unique_paths if any(e in p for e in expected_paths)]) / max(1, required_path_count))

    if deliverable == "path_decision":
        deliverable_score = (0.70 * path_score) + (0.20 * fact_score) + (0.10 * precision_score)
    elif deliverable == "source_evidence":
        deliverable_score = (0.40 * path_score) + (0.45 * fact_score) + (0.15 * precision_score)
    elif deliverable == "multi_file_explanation":
        deliverable_score = (0.35 * path_score) + (0.40 * fact_score) + (0.15 * enough_paths) + (0.10 * precision_score)
    else:
        deliverable_score = (0.35 * path_score) + (0.35 * fact_score) + (0.20 * enough_paths) + (0.10 * precision_score)

    retrieval_quality = float(final_score.get("sufficiency", 0.0))
    stop_quality = 1.0 if retrieval_quality >= sufficient_threshold else min(0.8, retrieval_quality)
    hallucination_guard = min(path_score, fact_score)
    final_quality = (
        (0.55 * deliverable_score)
        + (0.20 * retrieval_quality)
        + (0.15 * hallucination_guard)
        + (0.10 * stop_quality)
    )
    return {
        "deliverable": deliverable,
        "path_decision_score": round(path_score, 3),
        "fact_support_score": round(fact_score, 3),
        "noise_control_score": round(precision_score, 3),
        "task_completion_quality": round(final_quality, 3),
        "work_completed": final_quality >= sufficient_threshold,
    }


def _read_budget(effort_name: str) -> tuple[int, int]:
    if effort_name == "low":
        return 1, 4_000
    if effort_name == "medium":
        return 3, 8_000
    return 5, 16_000


def _adaptive_effort_for_task(task: dict[str, Any]) -> str:
    """Model the effort a disciplined agent should choose for this task."""

    level = str(task.get("abstract_level", ""))
    difficulty = str(task.get("difficulty", ""))
    if level == "locate":
        # Path-only locate calls are cheap; prefer higher recall over top-3 misses.
        return "high"
    if difficulty == "easy":
        return "low"
    if level == "snippet" or difficulty == "medium":
        return "medium"
    return "high"


def _candidate_globs(paths: list[str], max_paths: int = 5) -> list[str]:
    """Turn returned candidate paths into bounded rg include globs.

    This models a sensible agent policy: once skygrep has proposed likely
    anchors, fallback grep should search around those anchors before dumping
    the whole repository.
    """

    globs: list[str] = []
    for rel in _dedupe(paths)[:max_paths]:
        globs.append(Path(rel).as_posix())
    return _dedupe(globs)


def _needs_skygrep_extraction(paths: list[str]) -> bool:
    """Return true for candidate files that normal source reads may not parse."""

    extraction_suffixes = {
        ".doc",
        ".docx",
        ".epub",
        ".pages",
        ".pdf",
        ".ppt",
        ".pptx",
        ".rtf",
        ".xls",
        ".xlsx",
    }
    return any(Path(path).suffix.lower() in extraction_suffixes for path in paths)


def _closed_loop(
    root: Path,
    task: dict[str, Any],
    effort_name: str,
    *,
    policy: str,
    timeout: float,
    sufficient_threshold: float,
    allow_root_fallback: bool,
) -> dict[str, Any]:
    effort = EFFORTS[effort_name]
    payloads: list[str] = []
    paths: list[str] = []
    steps: list[dict[str, Any]] = []
    started = time.perf_counter()
    stop_reason = "budget_exhausted"

    def add_step(step: StepResult) -> dict[str, Any]:
        payloads.append(step.payload)
        paths.extend(step.paths)
        score = _current_score(task, payloads, paths)
        compact = step.compact()
        compact.update(
            {
                "path_coverage": score["path_coverage"],
                "evidence_coverage": score["evidence_coverage"],
                "sufficiency": score["sufficiency"],
            }
        )
        steps.append(compact)
        return score

    def enough(score: dict[str, Any]) -> bool:
        return float(score["sufficiency"]) >= sufficient_threshold

    known_scope = task.get("include", []) if effort.get("use_include") else []
    read_files, read_chars = _read_budget(effort_name)
    initial_needs_content = task.get("abstract_level") != "locate"

    if policy == "skygrep-first":
        score = add_step(
            _skygrep_step(
                root,
                task["query"],
                timeout=timeout,
                top=int(effort["top"]),
                detail=str(effort["detail"]),
                content=initial_needs_content,
                includes=known_scope,
                name="skygrep:initial",
            )
        )
        if enough(score):
            stop_reason = "sufficient_after_skygrep_initial"
        else:
            top_paths = paths[: max(1, min(read_files, len(paths)))]
            if top_paths:
                score = add_step(
                    _read_paths_step(
                        root,
                        top_paths,
                        max_files=read_files,
                        max_chars=read_chars,
                        name="skygrep:read_candidate_files",
                    )
                )
            if enough(score):
                stop_reason = "sufficient_after_candidate_file_reads"
            if (
                stop_reason == "budget_exhausted"
                and top_paths
                and effort_name == "high"
                and score["path_coverage"] > 0
                and _needs_skygrep_extraction(top_paths)
            ):
                score = add_step(
                    _skygrep_step(
                        root,
                        task["query"],
                        timeout=timeout,
                        top=max(3, int(effort["top"])),
                        detail="full",
                        includes=top_paths,
                        name="skygrep:extract_focused_full",
                    )
                )
            if enough(score) and stop_reason == "budget_exhausted":
                stop_reason = "sufficient_after_focused_extraction"
            if stop_reason == "budget_exhausted" and float(score["path_coverage"]) < 1.0:
                probe_scope = known_scope
                probe = _rg_path_probe_step(
                    root,
                    task["query"],
                    timeout=timeout,
                    terms=max(3, int(effort["rg_terms"])),
                    max_paths=max(8, int(effort["top"]) * 3),
                    includes=probe_scope,
                    name="fallback:rg_path_probe",
                )
                score = add_step(probe)
                if probe.paths:
                    score = add_step(
                        _read_paths_step(
                            root,
                            probe.paths,
                            max_files=read_files,
                            max_chars=read_chars,
                            name="fallback:read_probe_paths",
                        )
                    )
                if enough(score):
                    stop_reason = "sufficient_after_path_probe"
            if stop_reason == "budget_exhausted":
                fallback_scope = known_scope or _candidate_globs(paths)
                score = add_step(
                    _rg_step(
                        root,
                        task["query"],
                        timeout=timeout,
                        terms=max(6, int(effort["rg_terms"])),
                        max_matches=max(10, int(effort["rg_matches"])),
                        context=max(2, int(effort["rg_context"])),
                        includes=fallback_scope,
                        name="fallback:rg_scoped",
                    )
                )
                if enough(score):
                    stop_reason = "sufficient_after_scoped_rg_fallback"
                else:
                    if allow_root_fallback:
                        score = add_step(
                            _rg_step(
                                root,
                                task["query"],
                                timeout=timeout,
                                terms=max(6, int(effort["rg_terms"])),
                                max_matches=max(10, int(effort["rg_matches"])),
                                context=max(2, int(effort["rg_context"])),
                                includes=[],
                                name="fallback:rg_root_last_chance",
                            )
                        )
                    if enough(score):
                        stop_reason = "sufficient_after_root_rg_fallback"
                    else:
                        score = add_step(
                            _read_paths_step(
                                root,
                                paths,
                                max_files=read_files,
                                max_chars=read_chars,
                                name="fallback:read_top_paths",
                            )
                        )
                        if enough(score):
                            stop_reason = "sufficient_after_read_top_paths"
    elif policy == "rg-only":
        score = add_step(
            _rg_step(
                root,
                task["query"],
                timeout=timeout,
                terms=int(effort["rg_terms"]),
                max_matches=int(effort["rg_matches"]),
                context=int(effort["rg_context"]),
                includes=known_scope,
                name="rg:initial",
            )
        )
        if enough(score):
            stop_reason = "sufficient_after_rg_initial"
        else:
            score = add_step(
                _rg_step(
                    root,
                    task["query"],
                    timeout=timeout,
                    terms=max(10, int(effort["rg_terms"]) * 2),
                    max_matches=max(20, int(effort["rg_matches"]) * 2),
                    context=max(4, int(effort["rg_context"]) * 2),
                    includes=known_scope,
                    name="rg:expanded",
                )
            )
            if enough(score):
                stop_reason = "sufficient_after_rg_expanded"
            else:
                score = add_step(
                    _read_paths_step(
                        root,
                        paths,
                        max_files=read_files,
                        max_chars=read_chars,
                        name="rg:read_top_paths",
                    )
                )
                if enough(score):
                    stop_reason = "sufficient_after_read_top_paths"
    else:
        raise ValueError(f"unknown policy: {policy}")

    final_score = _current_score(task, payloads, paths)
    completion = _completion_quality(
        task,
        final_score,
        payloads,
        paths,
        sufficient_threshold=sufficient_threshold,
    )
    elapsed = time.perf_counter() - started
    return {
        "policy": policy,
        "effort": effort_name,
        "task_id": task["id"],
        "difficulty": task["difficulty"],
        "abstract_level": task["abstract_level"],
        "query": task["query"],
        "stop_reason": stop_reason,
        "steps": steps,
        "tool_calls": sum(int(s["tool_calls"]) for s in steps),
        "elapsed_seconds": round(elapsed, 3),
        "tool_elapsed_seconds": round(sum(float(s["elapsed_seconds"]) for s in steps), 3),
        "context_tokens": sum(int(s["context_tokens"]) for s in steps),
        "returned_paths": len(_dedupe(paths)),
        **final_score,
        **completion,
    }


def _pct(value: float) -> float:
    return round(100.0 * value, 1)


def _aggregate(
    rows: list[dict[str, Any]],
    tokens_per_second: float,
    sufficient_threshold: float,
) -> dict[str, Any]:
    if not rows:
        return {}
    n = len(rows)
    context_tokens = sum(int(r["context_tokens"]) for r in rows)
    elapsed = sum(float(r["elapsed_seconds"]) for r in rows)
    tool_elapsed = sum(float(r["tool_elapsed_seconds"]) for r in rows)
    sufficiency = sum(float(r["sufficiency"]) for r in rows) / n
    work_quality = sum(float(r["task_completion_quality"]) for r in rows) / n
    estimated_context_seconds = context_tokens / tokens_per_second if tokens_per_second > 0 else 0.0
    estimated_agent_elapsed = elapsed + estimated_context_seconds
    return {
        "tasks": n,
        "path_coverage_pct": _pct(sum(float(r["path_coverage"]) for r in rows) / n),
        "path_precision_pct": _pct(sum(float(r["path_precision"]) for r in rows) / n),
        "evidence_coverage_pct": _pct(sum(float(r["evidence_coverage"]) for r in rows) / n),
        "sufficiency_pct": _pct(sufficiency),
        "sufficient_tasks": sum(1 for r in rows if float(r["sufficiency"]) >= sufficient_threshold),
        "work_quality_pct": _pct(work_quality),
        "completed_tasks": sum(1 for r in rows if bool(r.get("work_completed"))),
        "tool_calls": sum(int(r["tool_calls"]) for r in rows),
        "elapsed_seconds": round(elapsed, 3),
        "tool_elapsed_seconds": round(tool_elapsed, 3),
        "avg_elapsed_seconds": round(elapsed / n, 3),
        "context_tokens": context_tokens,
        "avg_context_tokens": round(context_tokens / n, 1),
        "estimated_context_processing_seconds": round(estimated_context_seconds, 3),
        "estimated_agent_elapsed_seconds": round(estimated_agent_elapsed, 3),
        "sufficiency_per_1k_tokens": round((1000.0 * sufficiency) / max(1, context_tokens / n), 3),
        "work_quality_per_1k_tokens": round((1000.0 * work_quality) / max(1, context_tokens / n), 3),
        "work_quality_per_minute": round((60.0 * work_quality) / max(0.001, estimated_agent_elapsed / n), 3),
    }


def _compare_totals(sky: dict[str, Any], rg: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_call_reduction_x": round(rg["tool_calls"] / max(1, sky["tool_calls"]), 2),
        "context_token_reduction_x": round(rg["context_tokens"] / max(1, sky["context_tokens"]), 2),
        "elapsed_ratio_rg_over_skygrep": round(
            rg["elapsed_seconds"] / max(0.001, sky["elapsed_seconds"]), 2
        ),
        "estimated_agent_elapsed_ratio_rg_over_skygrep": round(
            rg["estimated_agent_elapsed_seconds"] / max(0.001, sky["estimated_agent_elapsed_seconds"]),
            2,
        ),
        "path_coverage_delta_pct": round(sky["path_coverage_pct"] - rg["path_coverage_pct"], 1),
        "evidence_coverage_delta_pct": round(sky["evidence_coverage_pct"] - rg["evidence_coverage_pct"], 1),
        "sufficiency_delta_pct": round(sky["sufficiency_pct"] - rg["sufficiency_pct"], 1),
        "work_quality_delta_pct": round(sky["work_quality_pct"] - rg["work_quality_pct"], 1),
        "completed_tasks_delta": sky["completed_tasks"] - rg["completed_tasks"],
        "sufficiency_density_ratio_skygrep_over_rg": round(
            sky["sufficiency_per_1k_tokens"] / max(0.001, rg["sufficiency_per_1k_tokens"]),
            2,
        ),
        "work_quality_density_ratio_skygrep_over_rg": round(
            sky["work_quality_per_1k_tokens"] / max(0.001, rg["work_quality_per_1k_tokens"]),
            2,
        ),
        "work_quality_per_minute_ratio_skygrep_over_rg": round(
            sky["work_quality_per_minute"] / max(0.001, rg["work_quality_per_minute"]),
            2,
        ),
    }


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    if args.refresh_index:
        _run(
            [sys.executable, "-m", "skylakegrep.src.cli", "index", str(root), "--incremental"],
            root,
            timeout=args.index_timeout,
        )
    tasks = json.loads(Path(args.tasks).read_text()) if args.tasks else DEFAULT_TASKS
    efforts = args.effort or list(EFFORTS)
    policies = args.policy or ["skygrep-first", "rg-only"]
    started = time.perf_counter()
    rows: list[dict[str, Any]] = []
    for effort_name in efforts:
        for task in tasks:
            for policy in policies:
                rows.append(
                    _closed_loop(
                        root,
                        task,
                        effort_name,
                        policy=policy,
                        timeout=args.timeout,
                        sufficient_threshold=args.sufficient_threshold,
                        allow_root_fallback=args.allow_root_fallback,
                    )
                )
    benchmark_elapsed = time.perf_counter() - started
    totals = {
        policy: _aggregate(
            [r for r in rows if r["policy"] == policy],
            args.tokens_per_second,
            args.sufficient_threshold,
        )
        for policy in policies
    }
    by_effort = {
        f"{policy}:{effort_name}": _aggregate(
            [r for r in rows if r["policy"] == policy and r["effort"] == effort_name],
            args.tokens_per_second,
            args.sufficient_threshold,
        )
        for policy in policies
        for effort_name in efforts
    }
    by_level = {
        f"{policy}:{level}": _aggregate(
            [r for r in rows if r["policy"] == policy and r["abstract_level"] == level],
            args.tokens_per_second,
            args.sufficient_threshold,
        )
        for policy in policies
        for level in sorted({r["abstract_level"] for r in rows})
    }
    adaptive_efforts = {task["id"]: _adaptive_effort_for_task(task) for task in tasks}
    adaptive_totals = {
        policy: _aggregate(
            [
                r
                for r in rows
                if r["policy"] == policy
                and r["effort"] == adaptive_efforts.get(r["task_id"])
            ],
            args.tokens_per_second,
            args.sufficient_threshold,
        )
        for policy in policies
    }
    comparison: dict[str, Any] = {}
    if "skygrep-first" in totals and "rg-only" in totals:
        comparison = _compare_totals(totals["skygrep-first"], totals["rg-only"])
    adaptive_comparison: dict[str, Any] = {}
    if "skygrep-first" in adaptive_totals and "rg-only" in adaptive_totals:
        adaptive_comparison = _compare_totals(
            adaptive_totals["skygrep-first"],
            adaptive_totals["rg-only"],
        )
    return {
        "definition": {
            "benchmark": "closed-loop agent retrieval benchmark",
            "skygrep_first": "depth-adaptive skygrep first: lightweight JSON for locate tasks, JSON/content for evidence tasks, direct candidate-file reads, path-only rg probes for missing anchors, skygrep extraction only for parsed documents, then bounded rg/read fallback if evidence is still insufficient",
            "rg_only": "raw rg term search, expanded rg search, then bounded file reads if evidence is still insufficient",
            "stop_rule": f"sufficiency >= {args.sufficient_threshold}, or policy budget exhausted",
            "quality_dimensions": "task-completion quality combines deliverable fit, required path decisions, fact support, noise control, retrieval sufficiency, and hallucination guard",
            "cost_dimensions": "actual elapsed time, tool calls, context tokens, quality per token, quality per minute; estimated agent elapsed adds context_tokens / tokens_per_second",
            "privacy": "default tasks are generic repository-maintenance tasks; output redacts the absolute benchmark root",
        },
        "parameters": {
            "root": "<benchmark-root>",
            "root_name": root.name,
            "tasks": len(tasks),
            "efforts": efforts,
            "policies": policies,
            "timeout_seconds": args.timeout,
            "sufficient_threshold": args.sufficient_threshold,
            "tokens_per_second": args.tokens_per_second,
            "allow_root_fallback": args.allow_root_fallback,
            "benchmark_wall_seconds": round(benchmark_elapsed, 3),
        },
        "totals": totals,
        "comparison": comparison,
        "adaptive_effort_plan": adaptive_efforts,
        "adaptive_totals": adaptive_totals,
        "adaptive_comparison": adaptive_comparison,
        "by_effort": by_effort,
        "by_abstract_level": by_level,
        "rows": [_compact_row(r) for r in rows] if args.summary_only else rows,
    }


def _compact_row(row: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "policy",
        "effort",
        "task_id",
        "difficulty",
        "abstract_level",
        "stop_reason",
        "tool_calls",
        "elapsed_seconds",
        "tool_elapsed_seconds",
        "context_tokens",
        "returned_paths",
        "path_coverage",
        "path_precision",
        "evidence_coverage",
        "sufficiency",
        "deliverable",
        "path_decision_score",
        "fact_support_score",
        "noise_control_score",
        "task_completion_quality",
        "work_completed",
        "missing_paths",
        "missing_evidence_terms",
        "steps",
    }
    return {k: v for k, v in row.items() if k in keep}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Closed-loop skygrep-vs-rg agent benchmark.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--tasks", help="Optional JSON task fixture")
    parser.add_argument("--effort", choices=sorted(EFFORTS), action="append")
    parser.add_argument("--policy", choices=["skygrep-first", "rg-only"], action="append")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--sufficient-threshold", type=float, default=0.85)
    parser.add_argument(
        "--tokens-per-second",
        type=float,
        default=30_000.0,
        help="Optional context processing model for estimated agent elapsed time.",
    )
    parser.add_argument("--refresh-index", action="store_true")
    parser.add_argument("--index-timeout", type=float, default=180.0)
    parser.add_argument(
        "--allow-root-fallback",
        action="store_true",
        help="Let skygrep-first do a broad final rg fallback. Disabled by default to measure compact-agent behavior.",
    )
    parser.add_argument("--summary-only", action="store_true")
    return parser.parse_args()


def main() -> None:
    print(json.dumps(run_benchmark(parse_args()), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
