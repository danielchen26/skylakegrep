# skylakegrep 0.7.0 — strict evidence and direct/daemon parity

0.7.0 is an accuracy-and-efficiency release for agent workflows. It adds a
fail-closed verification mode for high-risk local claims, makes direct and
daemon agent-context calls execute the same retrieval contract, and turns a
deterministic accuracy/performance comparison into a pull-request CI gate.

The release does not claim that fast retrieval proves external truth.
`--strict` verifies agreement and freshness inside the indexed local corpus;
agents must still use authoritative external sources for claims about the
outside world.

## What changed

- `skygrep --strict "<query>"` now implies agent context, runs the normal
  hybrid candidate-recall path plus an independent corpus-wide semantic pass,
  validates source/index modification-time parity, and emits a
  `strict_verification` receipt.
- Strict mode exits `2` when evidence is absent, the indexed source is stale,
  independent retrieval lanes disagree, semantic rank is insufficient, or the
  final agent quality remains inconclusive. Only `status=passed` is verified.
- Direct and HTTP-daemon agent-context calls share
  `run_agent_context_search`, including semantic escalation, evidence bundles,
  confidence summaries, source priors, and strict verification.
- The daemon rejects requests that point at a different project root or index
  database, and rejects lexical roots outside the configured project.
- Candidate recall coalesces equivalent macOS `/var` and `/private/var` path
  spellings before ranking. Duplicate aliases contribute only the strongest
  extra score, so lexical evidence is not double-counted.
- Duplicate results retain the union of their `candidate_recall_lanes`, making
  the final provenance receipt reflect every contributing lane.
- `agent-guidance-v5` teaches Claude Code, Codex, OpenCode, Gemini CLI, and
  Cursor to require strict receipts for security, release, legal, financial,
  destructive, and other high-risk local claims.
- Pull-request CI runs `benchmarks/ci_agent_contract_benchmark.py` through the
  existing closed-loop regression gate on Python 3.12.

## Measured performance and accuracy

The new CI fixture is deterministic and model-free. It exercises the real
hybrid candidate-recall implementation over six generic repository tasks, then
compares the compact skygrep-first contract with a modeled raw-ripgrep agent
workflow. This is a regression fixture, not a universal speed claim.

| Metric | skygrep-first | modeled raw-`rg` workflow | Result |
|---|---:|---:|---|
| Completed tasks | 6 / 6 | 6 / 6 | no completion regression |
| Path coverage | 100% | 100% | target paths preserved |
| Evidence coverage | 100% | 100% | required evidence preserved |
| Sufficiency | 100% | 100% | all tasks actionable |
| Context tokens | 14,689 | 59,551 | 4.05x less context |
| Tool calls | 6 | 99 | one skygrep call per task |
| Estimated agent elapsed | 293.85 s | 1,191.27 s | 4.05x lower modeled elapsed |
| Work quality / minute | 0.2042 | 0.0504 | 4.05x higher |

Live verification also ran the same strict query through the direct and daemon
paths. Top-K paths, candidate-recall lanes, agent quality, and strict status
were identical; both selected the expected implementation file and returned
`strict_verification.status=passed`.

## Accuracy and evidence boundaries

- Strict verification requires independent hybrid and semantic agreement; it
  does not accept first-pass confidence alone.
- Freshness compares the indexed source timestamp with the file currently on
  disk. A stale source produces an inconclusive receipt and exit `2`.
- The receipt exposes checks, independent lanes, semantic rank, evidence
  presence, source state, and an error string for machine-readable auditing.
- Direct/daemon parity is asserted by both an HTTP integration test and a live
  local daemon comparison.
- Daemon project-boundary validation prevents a caller from silently using the
  server with a different repository database.
- External factual claims remain outside this guarantee. Strict mode validates
  local evidence, not the current state of a website, service, law, or market.

## Compatibility

- Existing 0.6.x indexes are byte-compatible; no rebuild is required.
- The top-level agent JSON value remains a list of result objects.
  `strict_verification` and its summary status are optional additive fields.
- `--strict` is incompatible with answer/agentic synthesis modes because it is
  an evidence-verification contract, not a generation mode.
- Non-strict agent presets retain their existing bounded-latency defaults.
- Raw regular expressions and exhaustive lexical output should still use `rg`.

## Verification

- Full implementation suite: `390 passed, 20 subtests passed`, with two
  existing Click deprecation warnings.
- Strict-mode tests cover fresh-source success and stale-source rejection.
- Direct/daemon tests cover shared-contract parity, successful and
  inconclusive strict exit codes, and cross-project request rejection.
- macOS path-alias regression confirms coalescing without lexical score
  double-counting.
- The six-task CI gate passed with 100% path coverage, evidence coverage, and
  sufficiency; no configured threshold failed.
- Source compilation and `git diff --check` passed.
- The local index was rebuilt and independently queried as 190/190 absolute
  indexed paths before the release candidate was prepared.

## Privacy

Source, documentation, tests, benchmark fixtures, and the release candidate
use only generic repository-maintenance examples. The source tree and built
wheel/sdist are scanned before publication. No real local prompt, private
filename or folder, absolute user path, screenshot, document category, name,
email, credential, token, or machine-specific identifier is included.

## Known follow-ups

- The CI benchmark is deliberately small, deterministic, and model-free. The
  larger real-repository/Ollama benchmark remains the release-scale empirical
  check rather than a per-PR dependency.
- PyPI Trusted Publishing still depends on the project-side publisher matching
  owner `danielchen26`, repository `skylakegrep`, workflow `release.yml`, and
  environment `pypi`. If that external configuration rejects the tag job, the
  authenticated fallback remains required before the release can be called
  complete.
