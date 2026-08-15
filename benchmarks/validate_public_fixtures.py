"""Validate public benchmark pins, paths, and evidence without searching."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.public_fixtures import load_registry, prepare_repo, validate_repo_fixture


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oss-root", type=Path, default=Path("/tmp/skygrep-general-v2-repos"))
    parser.add_argument("--repo", action="append", help="Validate one repository key; repeatable.")
    parser.add_argument("--prepare", action="store_true", help="Clone/check out missing or mismatched pins.")
    parser.add_argument("--timeout", type=float, default=600.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    registry = load_registry()
    keys = args.repo or sorted(registry)
    rows = []
    failed = False
    for key in keys:
        spec = registry[key]
        repo = args.oss_root / spec.subdir
        if args.prepare:
            repo = prepare_repo(spec, args.oss_root, timeout=args.timeout)
        failures = (
            validate_repo_fixture(repo, spec)
            if repo.is_dir()
            else [f"repository is missing at <oss-root>/{spec.subdir}"]
        )
        failed = failed or bool(failures)
        rows.append(
            {
                "repo": key,
                "commit": spec.commit,
                "tasks": len(spec.tasks),
                "ok": not failures,
                "failures": failures,
            }
        )
    print(json.dumps({"ok": not failed, "repos": rows}, indent=2, sort_keys=True))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
