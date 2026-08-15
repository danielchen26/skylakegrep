# Public benchmark tasks

These fixtures are the reproducible public ground truth for the General
Benchmark v2. Every task is tied to a public repository and exact commit in
[`../public_repos.json`](../public_repos.json).

Each task must contain:

- a natural-language `question`;
- one canonical `expected` repository-relative file and optional
  `expected_alternatives`;
- at least two literal `evidence_terms` found in an accepted source file;
- at least two `quality_terms` used by the deterministic completion scorer;
- a short `ground_truth_note` explaining why the path is canonical.

The benchmark validates the public origin URL, pinned commit, clean tracked
worktree, accepted paths, and evidence terms before measuring either policy.
Missing, modified, or stale fixtures fail loudly. Private repository tasks
remain in the gitignored `benchmarks/cross_repo/` directory and must never be
copied here.

Validate already-cloned repositories without running a search:

```bash
python benchmarks/validate_public_fixtures.py \
  --oss-root /tmp/skygrep-general-v2-repos
```
