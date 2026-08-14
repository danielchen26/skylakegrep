# skylakegrep 0.6.0 — faster indexing and authoritative agent evidence

0.6.0 is a performance-and-accuracy release for agent workflows. It removes
per-file embedding request overhead, keeps repeated daemon calls responsive,
and makes the evidence behind agent confidence and synthesized answers more
explicit.

The speed work does not relax retrieval quality. Closed-loop fixtures now
enforce path-only locate semantics, retrieval-only queries cannot claim
lexical confidence they did not earn, and local answer synthesis keeps current
policy/reference documents separate from unrelated plans or version snapshots.

## What changed

- Full, incremental, explicit-file, and watch indexing batch chunks across
  file boundaries before requesting embeddings. The default batch is 64 and
  is clamped to 1..512 through `SKYGREP_INDEX_BATCH_SIZE`.
- Watch mode refreshes file embeddings and removes stale chunks when a tracked
  file becomes empty.
- `skygrep doctor` checks optional dependencies without importing heavyweight
  reranker modules.
- `skygrep serve` binds before optional reranker initialization. Reranker
  warming is explicit and asynchronous through `--warm-reranker`.
- Agent summaries expose `confidence_basis` as either
  `lexical-and-retrieval` or `retrieval-only`; non-lexical queries receive no
  synthetic lexical coverage credit.
- Candidate-recall and source-prior handling are shared across the agent path,
  including a decisive but reversible prior against unnamed historical version
  snapshots.
- `--answer` filters unrelated snapshots, roadmaps, and planning notes when a
  current living policy/reference document leads. Exact-version queries and
  explicitly named supporting documents remain available.
- The closed-loop benchmark contract now represents locate tasks as path-only
  tasks and preserves numeric fixture evidence.
- GitHub Actions use Node 24-compatible `actions/checkout@v5` and
  `actions/setup-python@v6`.

## Measured performance

All comparisons below were run on the same repository and codebase family.
Indexing, hot CLI operations, and closed-loop agent quality are reported as
separate receipts.

| Metric | Previous behavior | 0.6.0 | Result |
|---|---:|---:|---:|
| Fresh index elapsed | 624.44 s | 74.71 s | 8.36x faster, about 88% lower elapsed |
| `doctor` median | heavyweight dependency import path | 0.216 s | lightweight discovery |
| Hot daemon `--agent-context` median | optional reranker on startup path | 0.372 s | server binds first |
| Medium closed-loop completion | contract contained path-task mismatches | 8 / 8 | no failed tasks |
| Medium path coverage | - | 100% | all target paths recovered |
| Medium evidence coverage | - | 100% | all evidence targets recovered |
| Medium sufficiency | - | 100% | all tasks sufficient |
| Medium work quality | - | 95.7% | bounded context retained |

The adaptive comparison also used 9.82x less context and had 3.34x lower
estimated agent elapsed than the raw-`rg` policy. Raw `rg` remains the correct
tool for exact regex and exhaustive raw lexical output.

## Accuracy and evidence boundaries

The release treats speed and correctness as independent gates:

- deterministic file/chunk ordering is preserved across embedding batches;
- exact-version queries retain their matching historical document while other
  versions of the same document are excluded from current-policy synthesis;
- historical tasks remain unchanged when no living authority leads;
- `confidence_basis` states which evidence family supports the summary;
- a live release-policy answer cited only `docs/RELEASING.md:106-121` after
  unrelated version and planning documents were filtered;
- original ranked sources remain visible below synthesized answers.

## Compatibility

- Existing indexes are byte-compatible; no rebuild is required.
- The top-level agent JSON value remains a list of result objects. New fields
  are optional.
- Daemon startup no longer warms the optional reranker by default. Workloads
  that rely on later reranked queries can opt in with
  `skygrep serve --warm-reranker`.
- Exact regex/raw-output workflows should continue to call `rg` directly.

## Verification

- Full implementation suite: `383 passed, 20 subtests passed` with two existing
  Click deprecation warnings; the isolated Python 3.9 release environment also
  completed `383 passed` with one environment-only LibreSSL warning.
- Targeted answer/document-policy suite: `7 passed`; the Python 3.9 enrichment
  lifecycle regression suite completed `4 passed`.
- Pull-request CI: Python 3.10, 3.11, and 3.12 all passed.
- Post-merge `master` CI: Python 3.10, 3.11, and 3.12 all passed.
- Medium closed-loop benchmark: 8/8 complete, 100% path coverage, 100%
  evidence coverage, 100% sufficiency, no failures.
- Live `--answer` release query completed with the canonical release document
  as its only source.

## Privacy

Source, documentation, and the actual GitHub PR diff passed the privacy gate.
Public examples use fictional paths and generic prompts. No real local prompt,
private filename or folder, absolute local path, screenshot, document category,
name, email, credential, token, or machine-specific identifier is included.
Wheel and sdist artifacts are scanned again after the release build.

## Known follow-ups

- PyPI Trusted Publishing still needs the one-time project-side publisher
  registration for owner `danielchen26`, repository `skylakegrep`, workflow
  `release.yml`, and environment `pypi`. Until that account setting is added,
  releases use the existing authenticated `twine` path after the same build,
  metadata, artifact, and privacy gates. This affects automation only, not the
  package contents or availability.
