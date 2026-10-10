"""Tool steps for the token-lean benchmark policies.

``skygrep-slim``
    The progressive loop taught by agent-guidance-v6: one ``--agent-slim``
    call (compact anchors + outline of the top 3 source files), then
    ``skygrep symbols`` on the next candidates three at a time, then cheap
    path/filename probes feeding more outline batches, one deep outline, and
    only then the full ``skygrep-first`` policy as a fallback. Every step
    stops as soon as the sufficiency gate passes, so the fallback guarantees
    the policy completes whatever ``skygrep-first`` completes.

``rg-agent``
    A realistic ripgrep agent rather than a term-OR dump: per-term
    ``rg -c`` with output capped at 250 lines (the default ``head_limit`` of
    common agent Grep tools), BM25-style file ranking, the same outline
    reader in batches of three, then reads capped at 2,000 lines (the default
    of common agent Read tools). It shares the outline reader with
    ``skygrep-slim`` so the comparison isolates retrieval.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from benchmarks.token_savings import approximate_tokens

AGENT_GREP_LINE_CAP = 250
AGENT_READ_LINE_CAP = 2000
BATCH = 3
SOURCE_SUFFIXES = None  # resolved lazily from the product to stay in sync


def _source_suffixes() -> frozenset[str]:
    global SOURCE_SUFFIXES
    if SOURCE_SUFFIXES is None:
        from skylakegrep.src.agent_payload import SOURCE_SUFFIXES as product_suffixes

        SOURCE_SUFFIXES = product_suffixes
    return SOURCE_SUFFIXES


def is_source(path: str) -> bool:
    return Path(path).suffix.lower() in _source_suffixes()


def batches(items: list[str], size: int = BATCH) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _cli(cmd_args: list[str], root: Path, timeout: float) -> tuple[str, str, int, float]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "skylakegrep.src.cli", *cmd_args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "", f"timeout after {timeout}s", 124, time.perf_counter() - started
    return proc.stdout, proc.stderr, proc.returncode, time.perf_counter() - started


def skygrep_slim_step(root: Path, query: str, *, timeout: float, top: int, includes: list[str], name: str, step_cls):
    """Run ``--agent-slim``; return (step, result_paths, outlined_paths)."""

    args = ["search", "--agent-slim", "--top", str(top), "--no-auto-index"]
    for pattern in includes or []:
        args += ["--include", pattern]
    stdout, stderr, rc, elapsed = _cli([*args, query], root, timeout)
    result_paths: list[str] = []
    outlined: list[str] = []
    try:
        payload = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        for item in payload.get("results", []):
            if item.get("path") and item["path"] not in result_paths:
                result_paths.append(item["path"])
        outlined = [o["path"] for o in payload.get("outline", []) if o.get("path")]
    step = step_cls(
        name=name,
        tool_calls=1,
        elapsed_seconds=elapsed,
        context_tokens=approximate_tokens(stdout or stderr, 4),
        paths=list(dict.fromkeys([*result_paths, *outlined])),
        payload=stdout,
        returncode=rc,
        error=stderr if rc else "",
    )
    return step, result_paths, outlined


def symbols_step(root: Path, paths: list[str], query: str, *, budget: int, timeout: float, name: str, step_cls):
    """Run ``skygrep symbols --json`` on a batch; return (step, outlined_paths)."""

    stdout, stderr, rc, elapsed = _cli(
        ["symbols", *paths, "-q", query, "--budget", str(budget), "--max-files", str(len(paths)), "--json"],
        root,
        timeout,
    )
    outlined: list[str] = []
    try:
        payload = json.loads(stdout) if stdout.strip() else {}
        outlined = [o["path"] for o in payload.get("outline", []) if o.get("path")]
    except json.JSONDecodeError:
        pass
    step = step_cls(
        name=name,
        tool_calls=1,
        elapsed_seconds=elapsed,
        context_tokens=approximate_tokens(stdout or stderr, 4),
        paths=outlined,
        payload=stdout,
        returncode=rc,
        error=stderr if rc else "",
    )
    return step, outlined


def rg_rank_step(root: Path, terms: list[str], *, includes: list[str], name: str, step_cls, timeout: float = 30.0):
    """Per-term ``rg -c`` (agent-visible output capped), BM25-style ranking."""

    started = time.perf_counter()
    globs: list[str] = []
    for pattern in includes or []:
        globs += ["--glob", pattern]
    try:
        files = subprocess.run(
            ["rg", "--files", *globs], cwd=root, capture_output=True, text=True, timeout=timeout
        ).stdout.splitlines()
    except subprocess.TimeoutExpired:
        files = []
    n_files = max(1, len(files))
    score: dict[str, float] = defaultdict(float)
    sections: list[str] = []
    calls = 1
    for term in terms:
        calls += 1
        try:
            out = subprocess.run(
                ["rg", "-c", "-i", "-F", *globs, "--", term],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout,
            ).stdout
        except subprocess.TimeoutExpired:
            continue
        counts: dict[str, int] = {}
        for line in out.splitlines():
            path, _, count = line.rpartition(":")
            if path and count.isdigit():
                counts[path] = int(count)
        visible = sorted(counts.items(), key=lambda kv: -kv[1])[:AGENT_GREP_LINE_CAP]
        sections.append(f"## rg -c -i {term} ({len(counts)} files)\n" + "\n".join(f"{p}:{c}" for p, c in visible))
        idf = math.log(1 + n_files / (len(counts) + 1))
        for path, count in counts.items():
            score[path] += idf * (1 + math.log(count))
        for path in files:
            if term in path.lower():
                score[path] += 2 * idf
    ranked = sorted(score, key=lambda p: -score[p])
    payload = "\n\n".join(sections) or "NO_MATCHES"
    return (
        step_cls(
            name=name,
            tool_calls=calls,
            elapsed_seconds=time.perf_counter() - started,
            context_tokens=approximate_tokens(payload, 4),
            paths=ranked[:10],
            payload=payload,
            returncode=0,
        ),
        ranked,
    )


def read_lines_step(root: Path, path: str, *, name: str, step_cls, max_lines: int = AGENT_READ_LINE_CAP):
    started = time.perf_counter()
    target = (root / path).resolve()
    try:
        text = "\n".join(target.read_text(errors="ignore").splitlines()[:max_lines])
    except OSError:
        text = ""
    payload = f"## read file: {path}\n{text}" if text else "NO_READS"
    return step_cls(
        name=name,
        tool_calls=1,
        elapsed_seconds=time.perf_counter() - started,
        context_tokens=approximate_tokens(payload, 4),
        paths=[path] if text else [],
        payload=payload,
        returncode=0,
    )
