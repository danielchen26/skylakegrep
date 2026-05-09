# skylakegrep 0.5.12 — bounded cold semantic routing and public example verification

0.5.12 hardens the cold-start and agent-facing paths that matter most
when users copy examples directly from the README or GitHub Pages. It
keeps the 0.5.10/0.5.11 scoped-discovery behavior, then adds real
foreground budgets, better pruning, scoped agent examples, and a cleaner
answer-synthesis contract.

All examples below use fictional placeholders.

## What changed

- Cold-start semantic search now has separate foreground budgets for
  cwd lazy exploration, cross-folder lazy exploration, and foreground
  embedding. A slow sibling/home exploration lane can time out without
  blocking the answer already found in the current project.
- Lazy crawlers prune hidden folders and dependency caches before
  descent instead of discovering them and filtering afterward. This keeps
  broad searches out of editor caches, package-manager trees, and other
  irrelevant generated paths.
- Ripgrep cold fallback now runs one bounded multi-pattern pass instead
  of one subprocess per query term, reducing startup stalls for natural
  language questions.
- Warm cross-folder expansion no longer fires when the local project has
  semantic evidence. That prevents broad home-folder evidence from
  polluting a relevant local answer.
- Relative include globs such as `--include "src/**"` and
  `--include "docs/**"` now match absolute indexed paths, so examples
  used by LLM agents return the intended scoped evidence.
- The local answer prompt was tightened: when retrieved sources directly
  answer the question, `--answer` should answer from those sources
  without appending a contradictory missing-evidence caveat.
- The README, GitHub Pages homepage, CLI reference, setup instructions,
  and release notes now tell agents to pass `--include` or
  `--lexical-root` when they already know the scope.

## Before vs after

| Query shape | Previous behavior observed during validation | 0.5.12 behavior |
| --- | --- | --- |
| Broad cold semantic query from a large home-like folder | Could wait on cwd/cross-folder lazy futures for around 100 seconds | bounded foreground lazy budgets; returns partial/local evidence or timeout telemetry instead of hanging |
| Natural-language cold query with several words | Could run ripgrep once per term | one bounded ripgrep pass with multiple patterns |
| Local project has semantic evidence but confidence is not perfect | Could add unrelated sibling/home evidence | cross-folder expansion is skipped when local results exist |
| Agent uses `--include "src/**"` against an absolute index path | Could return no results | relative segment globs match absolute stored paths |
| `--answer` finds direct evidence | Could answer correctly and then add an unnecessary absence caveat | answers from direct evidence without the contradictory caveat |

## Public example verification

The public README / GitHub Pages command patterns were rerun on a
fictional local project containing code, docs, notes, hidden folders, and
dependency-cache folders. Representative shell wall times:

| Example pattern | Result |
| --- | ---: |
| `skygrep "where does the auth token get refreshed?"` | 1.42 s |
| `skygrep "where does session refresh logic live?"` | 4.58 s |
| `skygrep "the design doc on rate limiter rewrite"` | 0.38 s |
| `skygrep "我昨天写的 cascade 调度代码"` | 0.81 s |
| `skygrep --content --detail standard "what does the API migration plan say about rollback?"` | 0.36 s |
| `skygrep --content --detail full --include "docs/migration-plan.md" "show the deployment steps"` | 5.49 s |
| `skygrep --json --content --detail standard --include "src/**" "where is token refresh implemented?"` | 1.35 s |
| `skygrep --answer --content --include "docs/**" "summarize the payment retry policy"` | 2.58 s |
| Wrong-cwd proactive search with an explicit proactive root | 0.81 s |

These are not headline benchmark claims; they are release-gate receipts
that the examples users copy from public docs execute correctly and stay
bounded on a mixed synthetic project.

## Verification

- Full local test suite: `309 passed, 2 warnings, 20 subtests passed`.
- Targeted regression slice: `37 passed`, covering cold semantic
  timeout behavior, hidden/dependency pruning, relative include globs,
  setup-instruction scope guidance, and answer mode.
- Public example synthetic CLI run: 15 command patterns completed with
  expected anchors and bounded wall time.
- Source privacy scan: clean before build.
- Wheel/sdist privacy scan: required before upload.
- GitHub Actions test matrix: required after push.

## Compatibility

- No CLI flag was removed.
- Required JSON fields are unchanged.
- Existing indexes are compatible.
- New timeout and crawl-budget environment variables only cap foreground
  cold-start work. Background indexing can still build comprehensive
  coverage.
- Existing `skygrep setup` managed snippets continue to auto-refresh
  inside the BEGIN/END block only.

## Privacy note

The release notes, docs, README, tests, wheel, and sdist use fictional
examples only. No real user prompt, private filename, private folder
name, absolute local machine path, screenshot, document category, name,
or email is included in the public release surfaces.
