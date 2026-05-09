# skylakegrep 0.5.11 — fast scoped discovery plus Python 3.10 CI hotfix

0.5.11 supersedes 0.5.10. It keeps the fast scoped file-discovery and
agent instruction-depth work, then fixes a Python 3.10-only proactive
timeout exception that GitHub Actions caught after the 0.5.10 tag was
published.

All examples below use fictional placeholders.

## What changed

- Added a generic **scoped descriptor + metadata file-discovery lane**.
  When a query names a concrete scope, a target description, and a
  metadata modifier such as created / modified / opened, skygrep ranks
  files inside that bounded root by descriptor coverage, basename
  evidence, metadata evidence, hidden/generated-path penalties, extension
  priority, and depth.
- Descriptor-file evidence is final only for path/location answers.
  Content, `--answer`, and agentic queries continue into deeper
  retrieval instead of stopping at a file anchor.
- Large incremental refresh work defers to background indexing instead of
  blocking foreground search. The default foreground cap is 25 pending
  file changes and can be tuned with
  `SKYGREP_AUTO_REFRESH_MAX_FOREGROUND_FILES`.
- Human footer timing now reports command wall time from the `search`
  callback, including refresh/routing/scoped discovery foreground work.
- `skygrep setup` now writes the public information-depth ladder into
  Claude Code, Codex, OpenCode, Gemini CLI, and Cursor instructions.
- Existing `skygrep setup` snippets refresh automatically after upgrade.
  Normal `skygrep "<query>"` and `skygrep doctor` update only the
  previously managed BEGIN/END block; they never create a new agent
  integration or modify user-written text outside the marker.
- Fixed a Python 3.10 CI failure in the proactive enhancer runner by
  catching `concurrent.futures.TimeoutError` explicitly. Budget
  exhaustion is now telemetry, not an exception, across Python 3.10,
  3.11, and 3.12.

## Before vs after

| Query shape | Previous behavior observed during validation | 0.5.11 behavior |
| --- | --- | --- |
| Scoped file-location query with metadata modifier | Could fall through to semantic cascade and wait tens of seconds | `path=scoped-file-discovery`, semantic skipped, sub-second shell wall time |
| Warm query with many changed files | Could block behind foreground refresh before answering | large refresh deferred to background, search continues |
| Footer timing after foreground refresh | Could show only cascade-stage time | command wall time reported from `search` entry |
| Existing agent setup snippet after upgrade | Old instruction stayed in place until manual reinstall | managed block auto-refreshes on normal search / doctor |
| Proactive runner on Python 3.10 | Total-budget timeout could escape as an exception | timeout is handled and recorded as `timed_out` telemetry |

## Information-depth examples

```bash
skygrep "where is the project brief I edited recently?"
skygrep --content --detail standard "what does the API migration plan say about rollback?"
skygrep --content --detail full --include "docs/migration-plan.md" "show the deployment steps"
skygrep --answer --content "summarize the payment retry policy"
skygrep --json --content --detail standard "where is token refresh implemented?"
skygrep --explain "where is token refresh implemented?"
```

## Verification

- Full local test suite: `303 passed, 2 warnings, 20 subtests passed`.
- Targeted release regression slice: `31 passed`, covering proactive
  timeout handling, scoped discovery, foreground-refresh deferral, timing
  footer, and setup-snippet refresh.
- Generic synthetic CLI smoke: `path=scoped-file-discovery`,
  `semantic-skipped`, footer `0.058s`, shell `real 0.26s`.
- Source privacy scan: required before build.
- Wheel/sdist privacy scan: required before upload.
- GitHub Actions test matrix: required after push.

## Compatibility

- No CLI flag was removed.
- Required JSON fields are unchanged.
- No index rebuild is required.
- Existing agent instructions are refreshed only inside the
  `skygrep setup` managed marker block. Users who never ran setup are not
  auto-registered.
- The new foreground refresh cap affects latency, not recall: deferred
  files are picked up by background indexing, and exact/metadata/scoped
  foreground evidence still answers immediately when sufficient.

## Privacy note

All examples in this release use fictional placeholders such as
`project brief`, `API migration plan`, and `payment retry policy`. No real
user prompt, private filename, private folder name, local machine path,
screenshot, or document category is included in the release notes, tests,
docs, wheel, or sdist.
