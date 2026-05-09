# skylakegrep 0.5.10 — fast scoped file discovery and agent instruction depth

0.5.10 closes the gap between a fast local answer and a useful agent
answer. It keeps the 0.5.9 facet-based router, then hardens the paths
that real users hit immediately after the release:

- scoped file-location questions should return from filesystem evidence
  in under a second instead of falling through to semantic cascade;
- foreground search should not block behind hundreds of changed files;
- the footer should report the time the user actually waited;
- LLM agents should know when to ask for a path, snippets, full content,
  synthesized answers, or JSON.

All examples below use fictional placeholders.

## What changed

- Added a generic **scoped descriptor + metadata file-discovery lane**.
  When a query names a concrete scope, a target description, and a
  metadata modifier such as created / modified / opened, skygrep now ranks
  files inside that bounded root by descriptor coverage, basename evidence,
  metadata evidence, hidden/generated-path penalties, extension priority,
  and depth. This is not tied to any private query wording.
- Descriptor-file evidence is final only when the requested depth is a
  path/location answer. If the user asks what a file says, requests
  `--answer`, or uses agentic search, semantic retrieval remains alive.
- Large incremental refresh work now defers to background indexing instead
  of blocking the foreground search. The default foreground cap is 25
  pending file changes and can be tuned with
  `SKYGREP_AUTO_REFRESH_MAX_FOREGROUND_FILES`.
- Human footer timing now uses command wall time from the `search` callback
  instead of stage-local cascade timing. Refresh, routing, scoped discovery,
  and other foreground work are included, so the footer matches the user's
  wait much more closely.
- Fixed a no-cascade footer edge case where a semantic pool without the
  expected third counter could raise an index error.
- Documented the public **information-depth ladder**:
  bare `skygrep` for location, `--content --detail standard` for snippets,
  `--content --detail full --include ...` for scoped deep reading,
  `--answer --content` for local synthesis, and
  `--json --content --detail standard` for LLM/agent calls.
- `skygrep setup` now writes those depth rules into Claude Code, Codex,
  OpenCode, Gemini CLI, and Cursor instructions.
- Existing `skygrep setup` snippets now refresh automatically after
  upgrade. Normal `skygrep "<query>"` and `skygrep doctor` update only
  the previously managed BEGIN/END block; they never create a new agent
  integration or modify user-written text outside the marker.

## Before vs after

| Query shape | Previous behavior observed during validation | 0.5.10 behavior |
| --- | --- | --- |
| Scoped file-location query with metadata modifier | Could fall through to semantic cascade and wait tens of seconds | `path=scoped-file-discovery`, semantic skipped, sub-second shell wall time |
| Warm query with many changed files | Could block behind foreground refresh before answering | large refresh deferred to background, search continues |
| Footer timing after foreground refresh | Could show only cascade-stage time | command wall time reported from `search` entry |
| Existing agent setup snippet after upgrade | Old instruction stayed in place until manual reinstall | managed block auto-refreshes on normal search / doctor |
| Agent choosing output depth | Agent had only broad "prefer skygrep" guidance | setup snippet explains location, content, full-detail, answer, JSON, and explain modes |

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

- Full test suite: `303 passed, 2 warnings, 20 subtests passed`.
- Targeted release regression slice: `27 passed`.
- Targeted integration setup suite validates depth guidance, explicit
  setup refresh, and automatic managed-block refresh.
- Targeted scoped-discovery suite validates descriptor + metadata file
  discovery returns before embedding for path-depth questions.
- Timing regression validates displayed elapsed time includes foreground
  refresh delay.
- Generic synthetic CLI smoke: `path=scoped-file-discovery`,
  `semantic-skipped`, footer `0.103s`, shell `real 0.29s`.
- Source privacy scan: required before build.
- Wheel/sdist privacy scan: required before upload.

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
