"""Cross-project closed-loop benchmark for agent search workflows.

This runner measures whether a disciplined LLM agent gets enough context to
finish a task when it uses skygrep adaptively versus a raw-ripgrep baseline.
It is intentionally broader than the single-repo release benchmark:

* current project tasks cover snippet, deep, and abstract evidence workflows;
* public OSS fixtures cover conceptual file-location tasks across Python,
  JavaScript, and Rust projects;
* adaptive-only mode measures the depth a real agent should choose, instead of
  mechanically running low/medium/high for every task.

The benchmark still cannot prove all future projects will behave identically.
It gives a reproducible, multi-repo signal with explicit scope and metrics.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.agent_tool_depth_benchmark import DEFAULT_TASKS
from benchmarks.closed_loop_agent_benchmark import (
    _adaptive_effort_for_task,
    _aggregate,
    _closed_loop,
    _compare_totals,
)


@dataclass(frozen=True)
class RepoSpec:
    key: str
    label: str
    url: str
    subdir: str
    tasks: list[dict[str, Any]]


DJANGO_TASKS = [
    {
        "id": "django-url-dispatch",
        "question": "Where does Django turn an incoming URL into the view function that should handle it?",
        "expected": "django/urls/resolvers.py",
        "alternatives": ["django/urls/base.py", "django/core/handlers/base.py"],
    },
    {
        "id": "django-orm-sql-builder",
        "question": "Where is the ORM SQL query builder that translates QuerySet operations into raw SQL?",
        "expected": "django/db/models/sql/query.py",
        "alternatives": ["django/db/models/sql/compiler.py"],
    },
    {
        "id": "django-migration-runner",
        "question": "Where is the migration runner that applies pending schema changes to the database?",
        "expected": "django/db/migrations/executor.py",
        "alternatives": ["django/db/migrations/migration.py"],
    },
    {
        "id": "django-auth-backend",
        "question": "Where is the authentication backend that checks a username and password against the database?",
        "expected": "django/contrib/auth/backends.py",
        "alternatives": ["django/contrib/auth/__init__.py"],
    },
    {
        "id": "django-template-rendering",
        "question": "Where is the template rendering pipeline that turns a template file into an HTML string?",
        "expected": "django/template/engine.py",
        "alternatives": ["django/template/base.py", "django/template/loader.py"],
    },
    {
        "id": "django-csrf-token",
        "question": "Where does the CSRF middleware generate and validate the anti-forgery token in cookies?",
        "expected": "django/middleware/csrf.py",
        "alternatives": [],
    },
    {
        "id": "django-middleware-chain",
        "question": "Where is the request-handler middleware chain assembled before the first request?",
        "expected": "django/core/handlers/base.py",
        "alternatives": ["django/core/handlers/exception.py"],
    },
    {
        "id": "django-upload-handler",
        "question": "Where is the file upload handler that streams a multipart-form upload onto disk?",
        "expected": "django/core/files/uploadhandler.py",
        "alternatives": ["django/http/multipartparser.py"],
    },
    {
        "id": "django-connection-lifecycle",
        "question": "Where does Django determine if a connection should be reused or opened fresh per request?",
        "expected": "django/db/backends/base/base.py",
        "alternatives": ["django/db/__init__.py"],
    },
    {
        "id": "django-form-cleaning",
        "question": "Where is the form validation pass that runs all clean_<field> methods and aggregates errors?",
        "expected": "django/forms/forms.py",
        "alternatives": [],
    },
]


REACT_TASKS = [
    {
        "id": "react-use-state-storage",
        "question": "Where does React store and update the value returned by the useState hook between renders?",
        "expected": "packages/react-reconciler/src/ReactFiberHooks.js",
        "alternatives": [],
    },
    {
        "id": "react-fiber-work-loop",
        "question": "Where is the work loop that walks the fiber tree and decides which components to re-render?",
        "expected": "packages/react-reconciler/src/ReactFiberWorkLoop.js",
        "alternatives": ["packages/react-reconciler/src/ReactFiberBeginWork.js"],
    },
    {
        "id": "react-synthetic-events",
        "question": "Where is the synthetic event system that wraps native browser events before dispatching to a component handler?",
        "expected": "packages/react-dom-bindings/src/events/DOMPluginEventSystem.js",
        "alternatives": [
            "packages/react-dom-bindings/src/events/SyntheticEvent.js",
            "packages/react-dom/src/events/DOMPluginEventSystem.js",
        ],
    },
    {
        "id": "react-scheduler-priority",
        "question": "Where does the scheduler decide which task should run next based on priority lanes?",
        "expected": "packages/scheduler/src/forks/Scheduler.js",
        "alternatives": ["packages/react-reconciler/src/ReactFiberLane.js"],
    },
    {
        "id": "react-suspense-boundary",
        "question": "Where is the Suspense boundary code that catches a thrown promise during render and waits for it to resolve?",
        "expected": "packages/react-reconciler/src/ReactFiberSuspenseComponent.js",
        "alternatives": [
            "packages/react-reconciler/src/ReactFiberThrow.js",
            "packages/react-reconciler/src/ReactFiberUnwindWork.js",
        ],
    },
    {
        "id": "react-child-reconciliation",
        "question": "Where does React reconcile a list of children with key-based identity, deciding which fiber to reuse and which to discard?",
        "expected": "packages/react-reconciler/src/ReactChildFiber.js",
        "alternatives": [],
    },
    {
        "id": "react-create-element",
        "question": "Where is the implementation of React.createElement that the JSX runtime ultimately calls?",
        "expected": "packages/react/src/jsx/ReactJSXElement.js",
        "alternatives": ["packages/react/src/ReactElement.js"],
    },
    {
        "id": "react-context-propagation",
        "question": "Where does React propagate a context value down through the fiber tree when a Provider's value changes?",
        "expected": "packages/react-reconciler/src/ReactFiberNewContext.js",
        "alternatives": [],
    },
    {
        "id": "react-passive-effects",
        "question": "Where is the effect-flushing pass that runs useEffect callbacks after the browser has painted?",
        "expected": "packages/react-reconciler/src/ReactFiberCommitWork.js",
        "alternatives": ["packages/react-reconciler/src/ReactFiberWorkLoop.js"],
    },
    {
        "id": "react-profiler-timer",
        "question": "Where is the Profiler component implementation that measures render duration for telemetry?",
        "expected": "packages/react-reconciler/src/ReactProfilerTimer.js",
        "alternatives": ["packages/react/src/ReactProfiler.js"],
    },
]


TOKIO_TASKS = [
    {
        "id": "tokio-spawn-task",
        "question": "Where does Tokio actually launch a new asynchronous task on the runtime?",
        "expected": "tokio/src/task/spawn.rs",
        "alternatives": ["tokio/src/runtime/task/mod.rs", "tokio/src/runtime/handle.rs"],
    },
    {
        "id": "tokio-io-reactor",
        "question": "Where is the OS-level reactor that registers file descriptors for read/write readiness?",
        "expected": "tokio/src/runtime/io/driver.rs",
        "alternatives": ["tokio/src/io/poll_evented.rs"],
    },
    {
        "id": "tokio-mpsc-channel",
        "question": "Where is the multi-producer single-consumer channel that backs message passing between tasks?",
        "expected": "tokio/src/sync/mpsc/chan.rs",
        "alternatives": ["tokio/src/sync/mpsc/bounded.rs", "tokio/src/sync/mpsc/unbounded.rs"],
    },
    {
        "id": "tokio-async-mutex",
        "question": "Where does Tokio implement its asynchronous mutex with a fairness guarantee?",
        "expected": "tokio/src/sync/mutex.rs",
        "alternatives": [],
    },
    {
        "id": "tokio-sleep-timer",
        "question": "Where is the sleep / timer primitive backed by a hashed-wheel timer wheel?",
        "expected": "tokio/src/time/sleep.rs",
        "alternatives": ["tokio/src/runtime/time/wheel/mod.rs", "tokio/src/time/driver/mod.rs"],
    },
    {
        "id": "tokio-work-stealing",
        "question": "Where is the work-stealing thread-pool scheduler that powers the multi-threaded runtime?",
        "expected": "tokio/src/runtime/scheduler/multi_thread/worker.rs",
        "alternatives": [
            "tokio/src/runtime/scheduler/multi_thread/mod.rs",
            "tokio/src/runtime/scheduler/multi_thread/queue.rs",
        ],
    },
    {
        "id": "tokio-join-set",
        "question": "Where does Tokio implement structured concurrency primitives like JoinSet that wait for many tasks?",
        "expected": "tokio/src/task/join_set.rs",
        "alternatives": ["tokio/src/task/join_handle.rs"],
    },
    {
        "id": "tokio-notify",
        "question": "Where is the event-notification primitive used to signal one async task from another without sending a value?",
        "expected": "tokio/src/sync/notify.rs",
        "alternatives": [],
    },
    {
        "id": "tokio-ctrl-c",
        "question": "Where does the runtime intercept a Ctrl-C signal so async code can do graceful shutdown?",
        "expected": "tokio/src/signal/unix.rs",
        "alternatives": ["tokio/src/signal/ctrl_c.rs", "tokio/src/signal/windows.rs"],
    },
    {
        "id": "tokio-select-macro",
        "question": "Where is the macro that lets you await on multiple futures concurrently and pick the first one to complete?",
        "expected": "tokio/src/macros/select.rs",
        "alternatives": ["tokio-macros/src/select.rs"],
    },
]


PUBLIC_REPOS = {
    "django": RepoSpec(
        key="django",
        label="Django (Python)",
        url="https://github.com/django/django.git",
        subdir="django",
        tasks=DJANGO_TASKS,
    ),
    "react": RepoSpec(
        key="react",
        label="React (JS)",
        url="https://github.com/facebook/react.git",
        subdir="react",
        tasks=REACT_TASKS,
    ),
    "tokio": RepoSpec(
        key="tokio",
        label="Tokio (Rust)",
        url="https://github.com/tokio-rs/tokio.git",
        subdir="tokio",
        tasks=TOKIO_TASKS,
    ),
}


def _run(cmd: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True, timeout=timeout)


def _git_commit(repo: Path) -> str:
    try:
        proc = _run(["git", "rev-parse", "HEAD"], repo, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    return proc.stdout.strip() or "unknown"


def _repo_has_fixture_files(repo: Path, spec: RepoSpec) -> bool:
    for item in spec.tasks[:3]:
        candidates = [item["expected"], *item.get("alternatives", [])]
        if any((repo / str(candidate)).exists() for candidate in candidates):
            return True
    return False


def _ensure_repo(spec: RepoSpec, oss_root: Path, prepare: bool, timeout: float) -> Path | None:
    repo = oss_root / spec.subdir
    if repo.exists() and _repo_has_fixture_files(repo, spec):
        return repo
    if repo.exists():
        print(
            f"warning: {repo} exists but does not contain the public fixture paths; "
            "use a fresh --oss-root with --prepare",
            file=sys.stderr,
        )
        return None
    if not prepare:
        return None
    oss_root.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--depth", "1", spec.url, str(repo)]
    proc = _run(cmd, oss_root, timeout=timeout)
    if proc.returncode != 0:
        print(f"warning: failed to clone {spec.key}: {proc.stderr[-400:]}", file=sys.stderr)
        return None
    return repo


def _public_task(repo_key: str, item: dict[str, Any]) -> dict[str, Any]:
    alternatives = list(item.get("alternatives", []))
    expected = str(item["expected"])
    return {
        "id": f"{repo_key}-{item['id']}",
        "difficulty": "medium",
        "abstract_level": "locate",
        "deliverable": "path_decision",
        "query": str(item["question"]),
        "expected_paths": [expected],
        "expected_path_groups": [[expected, *alternatives]],
        "evidence_terms": [],
        "quality_terms": [],
        "min_paths": 1,
    }


def _project_tasks(limit: int | None) -> list[dict[str, Any]]:
    tasks = list(DEFAULT_TASKS)
    if limit is not None:
        tasks = tasks[:limit]
    return tasks


def _public_tasks(spec: RepoSpec, limit: int | None) -> list[dict[str, Any]]:
    tasks = [_public_task(spec.key, item) for item in spec.tasks]
    if limit is not None:
        tasks = tasks[:limit]
    return tasks


def _run_policy_rows(
    root: Path,
    tasks: list[dict[str, Any]],
    *,
    policies: list[str],
    timeout: float,
    sufficient_threshold: float,
    allow_root_fallback: bool,
    full_matrix: bool,
    trials: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trial in range(1, max(1, trials) + 1):
        for task in tasks:
            efforts = ["low", "medium", "high"] if full_matrix else [_adaptive_effort_for_task(task)]
            for effort in efforts:
                for policy in policies:
                    row = _closed_loop(
                        root,
                        task,
                        effort,
                        policy=policy,
                        timeout=timeout,
                        sufficient_threshold=sufficient_threshold,
                        allow_root_fallback=allow_root_fallback,
                    )
                    row["trial"] = trial
                    rows.append(row)
    return rows


def _summarize_rows(rows: list[dict[str, Any]], policies: list[str], tokens_per_second: float, threshold: float) -> dict[str, Any]:
    totals = {
        policy: _aggregate([row for row in rows if row["policy"] == policy], tokens_per_second, threshold)
        for policy in policies
    }
    comparison: dict[str, Any] = {}
    if "skygrep-first" in totals and "rg-only" in totals and totals["skygrep-first"] and totals["rg-only"]:
        comparison = _compare_totals(totals["skygrep-first"], totals["rg-only"])
    return {"totals": totals, "comparison": comparison}


def _compact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        "task_completion_quality",
        "work_completed",
        "missing_paths",
        "missing_evidence_terms",
        "steps",
        "trial",
    }
    return [{key: row[key] for key in keep if key in row} for row in rows]


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.perf_counter()
    policies = args.policy or ["skygrep-first", "rg-only"]
    sections: list[dict[str, Any]] = []
    all_rows: list[dict[str, Any]] = []

    if "self" in args.repo:
        tasks = _project_tasks(args.max_tasks_per_repo)
        rows = _run_policy_rows(
            PROJECT_ROOT,
            tasks,
            policies=policies,
            timeout=args.timeout,
            sufficient_threshold=args.sufficient_threshold,
            allow_root_fallback=args.allow_root_fallback,
            full_matrix=args.full_matrix,
            trials=args.trials,
        )
        all_rows.extend(rows)
        section = {
            "repo": "self",
            "label": "skylakegrep (Python CLI + docs)",
            "root": "<project-root>",
            "commit": _git_commit(PROJECT_ROOT),
            "tasks": len(tasks),
            **_summarize_rows(rows, policies, args.tokens_per_second, args.sufficient_threshold),
        }
        sections.append(section)

    for key in [repo for repo in args.repo if repo != "self"]:
        spec = PUBLIC_REPOS[key]
        repo = _ensure_repo(spec, args.oss_root, args.prepare, args.clone_timeout)
        if repo is None:
            sections.append(
                {
                    "repo": key,
                    "label": spec.label,
                    "skipped": True,
                    "reason": f"repo not found at {args.oss_root / spec.subdir}; rerun with --prepare to clone",
                }
            )
            continue
        tasks = _public_tasks(spec, args.max_tasks_per_repo)
        rows = _run_policy_rows(
            repo,
            tasks,
            policies=policies,
            timeout=args.timeout,
            sufficient_threshold=args.sufficient_threshold,
            allow_root_fallback=args.allow_root_fallback,
            full_matrix=args.full_matrix,
            trials=args.trials,
        )
        all_rows.extend(rows)
        sections.append(
            {
                "repo": key,
                "label": spec.label,
                "root": f"<oss-root>/{spec.subdir}",
                "commit": _git_commit(repo),
                "tasks": len(tasks),
                **_summarize_rows(rows, policies, args.tokens_per_second, args.sufficient_threshold),
            }
        )

    aggregate = _summarize_rows(all_rows, policies, args.tokens_per_second, args.sufficient_threshold)
    return {
        "definition": {
            "benchmark": "universal closed-loop agent search benchmark",
            "scope": "multi-repo adaptive workflow benchmark; strong generalization signal, not a proof over all future projects",
            "task_mix": "self repo has locate/snippet/deep/abstract tasks; public OSS repos have conceptual file-location tasks across Python, JavaScript, and Rust",
            "trust_model": "credible when the fixture repos, commits, task count, trials, and failure rows are reported; the benchmark is designed to expose misses rather than force a win",
            "policies": policies,
            "mode": "full-matrix low/medium/high" if args.full_matrix else "adaptive-only",
            "privacy": "public OSS tasks use public repository paths only; absolute roots are redacted",
        },
        "parameters": {
            "repos": args.repo,
            "oss_root": "<oss-root>",
            "max_tasks_per_repo": args.max_tasks_per_repo,
            "timeout_seconds": args.timeout,
            "sufficient_threshold": args.sufficient_threshold,
            "tokens_per_second": args.tokens_per_second,
            "allow_root_fallback": args.allow_root_fallback,
            "trials": args.trials,
            "benchmark_wall_seconds": round(time.perf_counter() - started, 3),
        },
        "aggregate": aggregate,
        "sections": sections,
        "rows": [] if args.summary_only else _compact_rows(all_rows),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Universal closed-loop skygrep benchmark.")
    parser.add_argument(
        "--repo",
        action="append",
        choices=["self", *sorted(PUBLIC_REPOS)],
        default=None,
        help="Repo fixture to run. Repeatable. Defaults to self + all public repos.",
    )
    parser.add_argument("--oss-root", type=Path, default=Path("/tmp/oss-bench"))
    parser.add_argument("--prepare", action="store_true", help="Clone missing public OSS repos into --oss-root.")
    parser.add_argument("--clone-timeout", type=float, default=600.0)
    parser.add_argument("--max-tasks-per-repo", type=int, default=None)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--sufficient-threshold", type=float, default=0.85)
    parser.add_argument("--tokens-per-second", type=float, default=30_000.0)
    parser.add_argument("--allow-root-fallback", action="store_true")
    parser.add_argument("--full-matrix", action="store_true")
    parser.add_argument("--policy", choices=["skygrep-first", "rg-only"], action="append")
    parser.add_argument("--summary-only", action="store_true")
    parser.add_argument("--trials", type=int, default=1, help="Repeat each policy/task/effort combination N times.")
    args = parser.parse_args()
    if args.repo is None:
        args.repo = ["self", *sorted(PUBLIC_REPOS)]
    return args


def main() -> None:
    print(json.dumps(run(parse_args()), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
