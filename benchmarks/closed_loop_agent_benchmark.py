# SPDX-License-Identifier: Apache-2.0
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
from typing import Any, Callable

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
    "answer",
    "anchor",
    "anchors",
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
    "code",
    "does",
    "entry",
    "for",
    "from",
    "function",
    "functions",
    "has",
    "have",
    "handle",
    "how",
    "identify",
    "identifies",
    "if",
    "incom",
    "incoming",
    "implementation",
    "implemented",
    "in",
    "is",
    "it",
    "into",
    "method",
    "of",
    "on",
    "or",
    "point",
    "prove",
    "proves",
    "responsibility",
    "routine",
    "routines",
    "should",
    "source",
    "symbol",
    "symbols",
    "that",
    "the",
    "this",
    "to",
    "turn",
    "turned",
    "turning",
    "turns",
    "type",
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

SYMBOL_DECLARATION_RE = re.compile(
    r"^\s*(?:(?:export|pub(?:\([^)]*\))?|public|protected|private|static|final|abstract|async)\s+)*"
    r"(?:def|class|func|function|fn|struct|enum|trait|interface|type|macro_rules!)\b",
    re.IGNORECASE,
)
JAVA_METHOD_RE = re.compile(
    r"^\s*(?:public|protected|private)\s+"
    r"(?:@\w+(?:\([^)]*\))?\s+)*"
    r"[\w<>,?.\[\]]+\s+[A-Za-z_$][\w$]*\s*\(",
)
CONTROL_FLOW_PREFIXES = (
    "catch ",
    "else ",
    "for ",
    "if ",
    "match ",
    "return ",
    "switch ",
    "while ",
)

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
    root = root.resolve()
    path = Path(raw_path)
    candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        return candidate.relative_to(root).as_posix()
    except (OSError, ValueError):
        if path.is_absolute():
            return f"<outside-benchmark-root>/{path.name}"
        return path.as_posix()


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
        raw_terms.extend(
            part
            for part in re.split(r"[-+]", lowered)
            if len(part) >= 3 and part != lowered
        )
        if len(lowered) > 3 and lowered.endswith("s"):
            raw_terms.append(lowered[:-1])
        if len(lowered) > 5 and lowered.endswith("ing"):
            raw_terms.append(lowered[:-3])
        if len(lowered) > 4 and lowered.endswith("ed"):
            raw_terms.append(lowered[:-2])
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
        error_summary = ""
        if self.error:
            error_summary = (
                self.error.strip()
                if self.error.strip().lower().startswith("timeout after")
                else "tool emitted stderr; inspect the private runner log"
            )
        return {
            "name": self.name,
            "tool_calls": self.tool_calls,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "context_tokens": self.context_tokens,
            "paths": len(self.paths),
            "returncode": self.returncode,
            **({"error": error_summary} if error_summary else {}),
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
                paths.append(_safe_rel(root, str(item["path"])))
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


def _path_word_match_score(term: str, word: str) -> int:
    if len(term) < 3 or len(word) < 3:
        return 0
    if term == word:
        return len(term) + 1
    if term in word or word in term:
        return min(len(term), len(word))
    common = 0
    for left, right in zip(term, word):
        if left != right:
            break
        common += 1
    return common if common >= 4 else 0


def _path_words(value: str) -> list[str]:
    expanded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return [word for word in re.findall(r"[a-z0-9]+", expanded.lower()) if len(word) >= 3]


def _filename_probe_step(
    root: Path,
    query: str,
    *,
    max_paths: int,
    name: str,
) -> StepResult:
    """Recall source paths whose filename or directory tokens match the query."""

    started = time.perf_counter()
    root = root.resolve()
    terms = _selected_probe_terms(root, query, max_terms=12)
    ranked: list[tuple[int, int, int, int, str, set[str]]] = []
    try:
        candidates = root.rglob("*")
        for target in candidates:
            if not target.is_file() or target.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            try:
                rel = target.relative_to(root).as_posix()
            except ValueError:
                continue
            path_words = _path_words(rel)
            filename_words = _path_words(target.stem)
            matched: set[str] = set()
            total_score = 0
            query_parts: set[str] = set()
            for term in terms:
                term_parts = [
                    part for part in re.findall(r"[a-z0-9]+", term.lower()) if len(part) >= 3
                ] or [term.lower()]
                query_parts.update(term_parts)
                best_path = max(
                    (_path_word_match_score(part, word) for part in term_parts for word in path_words),
                    default=0,
                )
                if best_path <= 0:
                    continue
                matched.add(term)
                total_score += best_path
            if not matched:
                continue
            filename_score = sum(
                max(
                    (_path_word_match_score(part, word) for part in query_parts),
                    default=0,
                )
                for word in set(filename_words)
            )
            lower = f"/{rel.lower()}"
            low_value_penalty = sum(1 for part in LOW_VALUE_PATH_PARTS if part in lower)
            adjusted_total_score = total_score - (8 * low_value_penalty)
            adjusted_filename_score = filename_score - (8 * low_value_penalty)
            ranked.append(
                (
                    -adjusted_filename_score,
                    -adjusted_total_score,
                    -len(matched),
                    low_value_penalty,
                    rel,
                    matched,
                )
            )
    except OSError:
        ranked = []
    ranked.sort(key=lambda item: item[:5])
    selected = ranked[:max_paths]
    paths = [item[4] for item in selected]
    payload = (
        "\n".join(
            f"{path}\tterms={','.join(sorted(item[5]))}"
            for item, path in zip(selected, paths)
        )
        if selected
        else "NO_FILENAME_MATCHES"
    )
    elapsed = time.perf_counter() - started
    return StepResult(
        name=name,
        tool_calls=1,
        elapsed_seconds=elapsed,
        context_tokens=approximate_tokens(payload, 4),
        paths=paths,
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
    root = root.resolve()
    sections: list[str] = []
    read_paths: list[str] = []
    for rel in _dedupe(paths)[:max_files]:
        raw_path = Path(rel)
        target = raw_path.resolve() if raw_path.is_absolute() else (root / raw_path).resolve()
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


def _symbol_declaration_priority(line: str) -> int:
    """Rank source declarations across the benchmark language matrix."""

    stripped = line.strip()
    lowered = stripped.lower()
    if SYMBOL_DECLARATION_RE.search(line) or JAVA_METHOD_RE.search(line):
        return 2
    if not stripped or lowered.startswith(CONTROL_FLOW_PREFIXES):
        return 0
    # Java/Kotlin/C#/Swift methods do not use a dedicated function keyword.
    # A bounded signature-shaped line is useful here; query-term filtering in
    # _read_symbol_paths_step keeps ordinary calls out of the final payload.
    return int(
        "(" in stripped
        and ")" in stripped
        and len(stripped) <= 300
        and (stripped.endswith("{") or stripped.endswith(";") or " throws " in lowered)
        and "=" not in stripped.split("(", 1)[0]
    )


def _looks_like_symbol_declaration(line: str) -> bool:
    return _symbol_declaration_priority(line) > 0


def _read_symbol_paths_step(
    root: Path,
    paths: list[str],
    query: str,
    *,
    max_files: int,
    max_chars_per_file: int,
    name: str,
) -> StepResult:
    """Read a bounded symbol inventory from candidate source files.

    File heads are a poor proxy for implementation evidence in large modules:
    the relevant function may sit thousands of lines below the imports. This
    pass scans only already-recalled candidate files, keeps declarations that
    share natural-language query terms, and returns compact line anchors. It
    never uses fixture answers or hidden evidence terms.
    """

    started = time.perf_counter()
    root = root.resolve()
    terms = _selected_probe_terms(root, query, max_terms=12)
    sections: list[str] = []
    read_paths: list[str] = []
    for rel in _dedupe(paths)[:max_files]:
        raw_path = Path(rel)
        target = raw_path.resolve() if raw_path.is_absolute() else (root / raw_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if not target.is_file() or target.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        try:
            lines = target.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        candidates: list[tuple[int, int, int, str]] = []
        for line_number, line in enumerate(lines, start=1):
            lowered = line.lower()
            hits = sum(1 for term in terms if term and term in lowered)
            strict_declaration = bool(
                SYMBOL_DECLARATION_RE.search(line) or JAVA_METHOD_RE.search(line)
            )
            if hits <= 0 and not strict_declaration:
                continue
            declaration_priority = _symbol_declaration_priority(line)
            # Query-matching declarations are most useful, but retain a
            # bounded inventory of strict declarations too. Natural-language
            # descriptions do not always reveal implementation names such as
            # updateSlot or Tx; an agent reading the candidate file still sees
            # those declarations without knowing fixture evidence in advance.
            priority = declaration_priority + (1 if hits > 0 else 0)
            candidates.append((-priority, -hits, line_number, line.rstrip()))
        rendered: list[str] = []
        used_chars = 0
        for _, _, line_number, line in sorted(candidates):
            item = f"{line_number}:{line}"
            if used_chars + len(item) + 1 > max_chars_per_file:
                continue
            rendered.append(item)
            used_chars += len(item) + 1
        if not rendered:
            continue
        sections.append(f"## relevant symbols: {rel}\n" + "\n".join(rendered))
        read_paths.append(rel)
    payload = "\n\n".join(sections) if sections else "NO_RELEVANT_SYMBOLS"
    elapsed = time.perf_counter() - started
    return StepResult(
        name=name,
        tool_calls=1 if read_paths else 0,
        elapsed_seconds=elapsed,
        context_tokens=approximate_tokens(payload, 4),
        paths=read_paths,
        payload=payload,
        returncode=0,
    )


def _candidate_scope_source_paths(
    root: Path,
    paths: list[str],
    query: str,
    *,
    max_scopes: int,
    max_files: int,
) -> list[str]:
    """Expand strong candidate directories to bounded immediate source siblings."""

    root = root.resolve()
    terms = _selected_probe_terms(root, query, max_terms=12)
    scope_stats: dict[str, dict[str, int]] = {}
    for rel in _dedupe(paths):
        raw_path = Path(rel)
        target = raw_path.resolve() if raw_path.is_absolute() else (root / raw_path).resolve()
        try:
            relative = target.relative_to(root)
        except ValueError:
            continue
        parent = relative.parent
        for hops in range(2):
            scope = parent.as_posix()
            stats = scope_stats.setdefault(
                scope,
                {
                    "term_hits": sum(1 for term in terms if term and term in scope.lower()),
                    "candidate_count": 0,
                    "hops": hops,
                    "depth": len(parent.parts),
                },
            )
            stats["candidate_count"] += 1
            stats["hops"] = min(stats["hops"], hops)
            if parent == Path(".") or parent.parent == parent:
                break
            parent = parent.parent
    ranked_scopes = sorted(
        scope_stats,
        key=lambda scope: (
            -scope_stats[scope]["term_hits"],
            -scope_stats[scope]["candidate_count"],
            scope_stats[scope]["hops"],
            -scope_stats[scope]["depth"],
            scope,
        ),
    )[:max_scopes]
    siblings: list[str] = []
    for scope in ranked_scopes:
        directory = root if scope == "." else root / scope
        try:
            children = sorted(directory.iterdir(), key=lambda path: path.name)
        except OSError:
            continue
        for child in children:
            if not child.is_file() or child.suffix.lower() not in SOURCE_SUFFIXES:
                continue
            siblings.append(child.relative_to(root).as_posix())
            if len(_dedupe(siblings)) >= max_files:
                return _dedupe(siblings)
    return _dedupe(siblings)


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


def _rank_of_first_hit(
    expected_groups: list[list[str]], paths: list[str]
) -> int | None:
    """1-indexed position of the first returned path that satisfies an
    expected group, or ``None`` when no returned path ever does.

    This is the axis ``path_precision`` structurally cannot see. Precision@k
    is bounded by ``relevant / k``: with the usual one relevant file and
    ``--top 8``, no retriever on earth can exceed 12.5%, so the metric grades
    the chosen top-k far more than it grades ranking. The cost an agent
    actually pays is how many wrong files it opens before the right one,
    which is exactly this rank — and the derived MRR / hit@1 / hit@3 are
    comparable across tools at a fixed k.

    Order matters here, so unlike :func:`_path_group_precision` the input
    list must not be sorted or set-deduplicated by the caller.
    """

    for index, path in enumerate(paths, start=1):
        if not path:
            continue
        if any(
            candidate and candidate in path
            for group in expected_groups
            for candidate in group
        ):
            return index
    return None


def _rank_metrics(expected_groups: list[list[str]], paths: list[str]) -> dict[str, Any]:
    rank = _rank_of_first_hit(expected_groups, _dedupe(paths))
    return {
        "rank_first_hit": rank,
        "reciprocal_rank": round(1.0 / rank, 3) if rank else 0.0,
        "hit_at_1": 1 if rank == 1 else 0,
        "hit_at_3": 1 if rank is not None and rank <= 3 else 0,
    }


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
            # Additive on purpose: sufficiency keeps its published definition so
            # receipts recorded before rank metrics existed stay comparable.
            "sufficiency": round((0.6 * path_coverage) + (0.4 * evidence_coverage), 3),
            **_rank_metrics(expected_groups, paths),
            "missing_paths": _missing_path_groups(expected_groups, paths),
            "missing_evidence_terms": [
                term for term in evidence_terms if term.lower() not in payload
            ],
        }
    expected_paths = task.get("expected_paths", [])
    return {
        **_score_context(expected_paths, evidence_terms, _dedupe(paths), payload),
        **_rank_metrics([[path] for path in expected_paths], paths),
    }


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


@dataclass
class PolicyContext:
    """Everything a retrieval policy may touch, and nothing else.

    The closed loop owns accumulation, scoring, and stopping; a policy only
    decides which retrieval and read steps to take, in what order, and when it
    is satisfied. Passing that surface explicitly is what lets a third-party
    tool become an arm without editing the loop — the previous shape was an
    ``if policy == ...`` chain, so only the two arms the author wrote could
    ever be measured, which makes a benchmark a product demo.

    ``paths`` is the live accumulator and is read-only for policies: it grows
    as a side effect of :attr:`add_step`, never by direct mutation.
    """

    root: Path
    task: dict[str, Any]
    effort_name: str
    effort: dict[str, Any]
    timeout: float
    allow_root_fallback: bool
    known_scope: list[str]
    read_files: int
    read_chars: int
    initial_needs_content: bool
    paths: list[str]
    add_step: Callable[[StepResult], dict[str, Any]]
    enough: Callable[[dict[str, Any]], bool]


@dataclass(frozen=True)
class PolicySpec:
    """A measurable arm. ``run`` returns the stop reason it settled on."""

    name: str
    summary: str
    #: Executable the arm needs, for cross-referencing
    #: :mod:`benchmarks.dependency_preflight`. Empty when the arm is in-process.
    binary: str
    run: Callable[[PolicyContext], str]
    #: Whether this arm returns results in a relevance order. False for a
    #: scanner: ripgrep emits matches in traversal order, so MRR and hit@k
    #: computed over its output are not a worse ranking, they are an invented
    #: number — and because rg walks in parallel without ``--sort``, that
    #: number is unstable run to run (measured: hit@1 50.0 then 0.0 on an
    #: unchanged tree). Rank axes are reported as null for unranked arms
    #: instead of being stabilised with ``--sort path``, which would disable
    #: rg's parallelism and inflate the latency comparison in our favour.
    ranked: bool = True


POLICIES: dict[str, PolicySpec] = {}

#: Arms measured when --policy is not given. The published General
#: Benchmark compares exactly these two, so the default is pinned rather
#: than "everything registered": adding an arm must not silently change
#: what a bare invocation reports.
DEFAULT_POLICIES = ("skygrep-first", "rg-only")


def register_policy(spec: PolicySpec) -> PolicySpec:
    if spec.name in POLICIES:
        raise ValueError(f"policy {spec.name!r} is already registered")
    POLICIES[spec.name] = spec
    return spec


def policy_names() -> list[str]:
    return sorted(POLICIES)


def get_policy(name: str) -> PolicySpec:
    try:
        return POLICIES[name]
    except KeyError:
        raise ValueError(
            f"unknown policy {name!r}; registered: {', '.join(policy_names()) or 'none'}"
        ) from None


def _policy_skygrep_first(ctx: PolicyContext) -> str:
    """skygrep first, then read candidates, then probe, then fall back."""

    root, task, effort = ctx.root, ctx.task, ctx.effort
    effort_name, timeout = ctx.effort_name, ctx.timeout
    allow_root_fallback = ctx.allow_root_fallback
    known_scope, read_files, read_chars = ctx.known_scope, ctx.read_files, ctx.read_chars
    initial_needs_content = ctx.initial_needs_content
    paths, add_step, enough = ctx.paths, ctx.add_step, ctx.enough
    stop_reason = "budget_exhausted"

    probe_paths: list[str] = []
    filename_paths: list[str] = []
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
        if stop_reason == "budget_exhausted" and (
            float(score["path_coverage"]) < 1.0
            or float(score["evidence_coverage"]) < 1.0
        ):
            probe_scope = known_scope
            probe = _rg_path_probe_step(
                root,
                task["query"],
                timeout=timeout,
                terms=max(3, int(effort["rg_terms"])),
                max_paths=max(15, int(effort["top"]) * 5),
                includes=probe_scope,
                name="fallback:rg_path_probe",
            )
            probe_paths = probe.paths
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
        if stop_reason == "budget_exhausted" and initial_needs_content:
            filename_probe = _filename_probe_step(
                root,
                task["query"],
                max_paths=max(30, int(effort["top"]) * 10),
                name="fallback:filename_probe",
            )
            filename_paths = filename_probe.paths
            score = add_step(filename_probe)
            if enough(score):
                stop_reason = "sufficient_after_filename_probe"
        if stop_reason == "budget_exhausted" and paths and initial_needs_content:
            symbol_candidates = _dedupe([*paths, *probe_paths, *filename_paths])
            score = add_step(
                _read_symbol_paths_step(
                    root,
                    symbol_candidates,
                    task["query"],
                    max_files=max(50, int(effort["top"]) * 16),
                    max_chars_per_file=max(8_000, read_chars * 2),
                    name="fallback:read_candidate_symbols",
                )
            )
            if enough(score):
                stop_reason = "sufficient_after_candidate_symbol_reads"
        if stop_reason == "budget_exhausted" and paths and initial_needs_content:
            sibling_candidates = _candidate_scope_source_paths(
                root,
                [*paths, *probe_paths, *filename_paths],
                task["query"],
                max_scopes=5,
                max_files=80,
            )
            score = add_step(
                _read_symbol_paths_step(
                    root,
                    sibling_candidates,
                    task["query"],
                    max_files=80,
                    max_chars_per_file=4_000,
                    name="fallback:read_scoped_sibling_symbols",
                )
            )
            if enough(score):
                stop_reason = "sufficient_after_scoped_sibling_symbols"
        if stop_reason == "budget_exhausted":
            fallback_scope = known_scope or _candidate_globs(
                [*paths, *probe_paths, *filename_paths],
                max_paths=max(5, int(effort["top"]) * 2),
            )
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

    return stop_reason


def _policy_rg_only(ctx: PolicyContext) -> str:
    """Lexical baseline: rg, widen rg, then read the top paths."""

    root, task, effort = ctx.root, ctx.task, ctx.effort
    effort_name, timeout = ctx.effort_name, ctx.timeout
    allow_root_fallback = ctx.allow_root_fallback
    known_scope, read_files, read_chars = ctx.known_scope, ctx.read_files, ctx.read_chars
    initial_needs_content = ctx.initial_needs_content
    paths, add_step, enough = ctx.paths, ctx.add_step, ctx.enough
    stop_reason = "budget_exhausted"

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

    return stop_reason


register_policy(
    PolicySpec(
        name="skygrep-first",
        summary="semantic retrieval first, with lexical probes as fallback",
        binary="skygrep",
        run=_policy_skygrep_first,
    )
)
register_policy(
    PolicySpec(
        name="rg-only",
        summary="ripgrep term extraction only; the lexical floor",
        binary="rg",
        run=_policy_rg_only,
        ranked=False,
    )
)


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

    ctx = PolicyContext(
        root=root,
        task=task,
        effort_name=effort_name,
        effort=effort,
        timeout=timeout,
        allow_root_fallback=allow_root_fallback,
        known_scope=known_scope,
        read_files=read_files,
        read_chars=read_chars,
        initial_needs_content=initial_needs_content,
        paths=paths,
        add_step=add_step,
        enough=enough,
    )
    stop_reason = get_policy(policy).run(ctx)

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
    *,
    ranked: bool = True,
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
        # Rank-based axes. Reported next to precision, not folded into it:
        # precision@k is capped at relevant/k, so it cannot separate a tool
        # that ranks the answer first from one that ranks it eighth. Null for
        # arms that do not rank at all; see PolicySpec.ranked.
        "ranked_arm": ranked,
        "mrr": (
            round(sum(float(r.get("reciprocal_rank", 0.0)) for r in rows) / n, 3)
            if ranked
            else None
        ),
        "hit_at_1_pct": (
            _pct(sum(int(r.get("hit_at_1", 0)) for r in rows) / n) if ranked else None
        ),
        "hit_at_3_pct": (
            _pct(sum(int(r.get("hit_at_3", 0)) for r in rows) / n) if ranked else None
        ),
        "mean_rank_when_found": (
            None
            if not ranked
            else round(
                sum(
                    int(r["rank_first_hit"])
                    for r in rows
                    if r.get("rank_first_hit")
                )
                / max(1, sum(1 for r in rows if r.get("rank_first_hit"))),
                2,
            )
            if any(r.get("rank_first_hit") for r in rows)
            else None
        ),
        "tasks_never_found": (
            sum(1 for r in rows if not r.get("rank_first_hit")) if ranked else None
        ),
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
        # A rank delta against an unranked arm would compare a ranking to a
        # traversal order, so it is withheld rather than fabricated.
        **(
            {
                "mrr_delta": round(sky["mrr"] - rg["mrr"], 3),
                "hit_at_1_delta_pct": round(sky["hit_at_1_pct"] - rg["hit_at_1_pct"], 1),
                "hit_at_3_delta_pct": round(sky["hit_at_3_pct"] - rg["hit_at_3_pct"], 1),
            }
            if sky.get("mrr") is not None and rg.get("mrr") is not None
            else {"rank_delta_note": "baseline arm does not rank; no rank delta"}
        ),
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
    policies = args.policy or DEFAULT_POLICIES
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
            ranked=get_policy(policy).ranked,
        )
        for policy in policies
    }
    by_effort = {
        f"{policy}:{effort_name}": _aggregate(
            [r for r in rows if r["policy"] == policy and r["effort"] == effort_name],
            args.tokens_per_second,
            args.sufficient_threshold,
            ranked=get_policy(policy).ranked,
        )
        for policy in policies
        for effort_name in efforts
    }
    by_level = {
        f"{policy}:{level}": _aggregate(
            [r for r in rows if r["policy"] == policy and r["abstract_level"] == level],
            args.tokens_per_second,
            args.sufficient_threshold,
            ranked=get_policy(policy).ranked,
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
            ranked=get_policy(policy).ranked,
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
    parser.add_argument(
        "--policy",
        action="append",
        choices=policy_names(),
        help=(
            "arm to measure; repeatable. Choices come from the policy registry, "
            "so a newly registered arm is selectable without touching argparse: "
            + "; ".join(f"{s.name} ({s.summary})" for s in POLICIES.values())
        ),
    )
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
