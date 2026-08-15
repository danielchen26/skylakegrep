"""Public, pinned benchmark repository and task-fixture contracts."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = PROJECT_ROOT / "benchmarks" / "public_repos.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class RepoSpec:
    key: str
    label: str
    url: str
    subdir: str
    commit: str
    fixture: Path
    tasks: tuple[dict[str, Any], ...]


def _relative_public_path(raw: str, *, field: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must be a repository-relative public path: {raw!r}")
    return path


def _load_tasks(path: Path, repo_key: str) -> tuple[dict[str, Any], ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list) or not payload:
        raise ValueError(f"{path}: fixture must be a non-empty JSON list")

    tasks: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    required_text = (
        "id",
        "question",
        "expected",
        "difficulty",
        "abstract_level",
        "deliverable",
        "ground_truth_note",
    )
    for index, raw_task in enumerate(payload):
        if not isinstance(raw_task, dict):
            raise ValueError(f"{path}: task {index} must be an object")
        task = dict(raw_task)
        for field in required_text:
            value = task.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{path}: task {index} requires non-empty {field}")
        task_id = str(task["id"])
        if task_id in seen_ids:
            raise ValueError(f"{path}: duplicate task id {task_id!r}")
        seen_ids.add(task_id)

        _relative_public_path(str(task["expected"]), field="expected")
        alternatives = task.get("expected_alternatives", [])
        if not isinstance(alternatives, list):
            raise ValueError(f"{path}: {task_id} expected_alternatives must be a list")
        for candidate in alternatives:
            if not isinstance(candidate, str) or not candidate:
                raise ValueError(f"{path}: {task_id} has an invalid alternative path")
            _relative_public_path(candidate, field="expected_alternatives")

        for field in ("evidence_terms", "quality_terms"):
            values = task.get(field)
            if not isinstance(values, list) or len(values) < 2:
                raise ValueError(f"{path}: {task_id} requires at least two {field}")
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"{path}: {task_id} has an invalid {field} value")

        task["id"] = f"{repo_key}-{task_id}"
        tasks.append(task)
    return tuple(tasks)


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict[str, RepoSpec]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(payload.get("repos"), dict):
        raise ValueError(f"{path}: expected public repository schema_version 1")

    specs: dict[str, RepoSpec] = {}
    for key, raw in sorted(payload["repos"].items()):
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: repository {key!r} must be an object")
        commit = str(raw.get("commit", ""))
        if not SHA_RE.fullmatch(commit):
            raise ValueError(f"{path}: repository {key!r} requires a full 40-character commit")
        fixture_rel = _relative_public_path(str(raw.get("fixture", "")), field="fixture")
        fixture = PROJECT_ROOT / fixture_rel
        if not fixture.is_file():
            raise ValueError(f"{path}: missing public fixture {fixture_rel}")
        url = str(raw.get("url", ""))
        if not url.startswith("https://github.com/") or not url.endswith(".git"):
            raise ValueError(f"{path}: repository {key!r} must use a public GitHub clone URL")
        specs[key] = RepoSpec(
            key=key,
            label=str(raw.get("label", key)),
            url=url,
            subdir=str(_relative_public_path(str(raw.get("subdir", key)), field="subdir")),
            commit=commit,
            fixture=fixture,
            tasks=_load_tasks(fixture, key),
        )
    return specs


def git_commit(repo: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return proc.stdout.strip() if proc.returncode == 0 else "unknown"


def _git_stdout(repo: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout.strip()


def _normalized_git_url(url: str) -> str:
    return url.removesuffix(".git").rstrip("/")


def validate_repo_fixture(repo: Path, spec: RepoSpec, *, require_commit: bool = True) -> list[str]:
    failures: list[str] = []
    actual_commit = git_commit(repo)
    if require_commit and actual_commit != spec.commit:
        failures.append(f"commit {actual_commit} does not match pin {spec.commit}")
    remote_code, remote_url = _git_stdout(repo, "remote", "get-url", "origin")
    if remote_code != 0 or _normalized_git_url(remote_url) != _normalized_git_url(spec.url):
        failures.append("origin URL does not match the public registry")
    dirty_code, dirty_output = _git_stdout(repo, "status", "--porcelain", "--untracked-files=no")
    if dirty_code != 0:
        failures.append("could not verify the tracked worktree state")
    elif dirty_output:
        failures.append("tracked worktree differs from the pinned commit")

    for task in spec.tasks:
        accepted = [str(task["expected"]), *task.get("expected_alternatives", [])]
        existing = [repo / candidate for candidate in accepted if (repo / candidate).is_file()]
        if not existing:
            failures.append(f"{task['id']}: none of the accepted paths exist")
            continue
        combined = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in existing)
        for term in task["evidence_terms"]:
            if term not in combined:
                failures.append(f"{task['id']}: evidence term {term!r} is absent from accepted paths")
        for term in task["quality_terms"]:
            if term not in combined:
                failures.append(f"{task['id']}: quality term {term!r} is absent from accepted paths")
    return failures


def prepare_repo(spec: RepoSpec, oss_root: Path, *, timeout: float = 600.0) -> Path:
    """Clone or update one public repository to its exact published pin."""

    repo = oss_root / spec.subdir
    oss_root.mkdir(parents=True, exist_ok=True)
    newly_cloned = False
    if not repo.exists():
        proc = subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", spec.url, str(repo)],
            cwd=str(oss_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"failed to clone {spec.key}: {proc.stderr[-400:]}")
        newly_cloned = True
    if not (repo / ".git").exists():
        raise RuntimeError(f"{repo} exists but is not a Git repository")

    preflight = validate_repo_fixture(repo, spec, require_commit=False)
    provenance_failures = [failure for failure in preflight if failure.startswith("origin URL")]
    if not newly_cloned:
        provenance_failures.extend(
            failure for failure in preflight if failure.startswith("tracked worktree")
        )
    if provenance_failures:
        raise RuntimeError(f"{spec.key} repository preflight failed: {'; '.join(provenance_failures)}")

    if newly_cloned or git_commit(repo) != spec.commit:
        if git_commit(repo) != spec.commit:
            fetch = subprocess.run(
                ["git", "fetch", "--depth", "1", "origin", spec.commit],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if fetch.returncode != 0:
                raise RuntimeError(f"failed to fetch {spec.key} pin: {fetch.stderr[-400:]}")
        checkout = subprocess.run(
            ["git", "checkout", "--detach", spec.commit],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if checkout.returncode != 0:
            raise RuntimeError(f"failed to checkout {spec.key} pin: {checkout.stderr[-400:]}")

    failures = validate_repo_fixture(repo, spec)
    if failures:
        raise RuntimeError(f"{spec.key} fixture validation failed: {'; '.join(failures[:10])}")
    return repo


def get_spec(key: str, registry: Optional[dict[str, RepoSpec]] = None) -> RepoSpec:
    specs = registry or load_registry()
    try:
        return specs[key]
    except KeyError as exc:
        raise ValueError(f"unknown public repository {key!r}") from exc
