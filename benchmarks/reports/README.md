# Published benchmark receipts

This directory contains immutable, privacy-scanned benchmark receipts that
support public performance claims. A receipt records the benchmark source
commit, environment, exact public fixture commits, all success and failure
rows, and the aggregate gate result.

## Corrected agent-path rerun — 2026-09-17 (capacity-blocked)

These are **partial verification receipts, not a new six-repository performance
claim**. They were generated from clean source commit
`1838da04e6f8e18c82528faf2a19edddbde72ddf`, which integrates the latency-path
correction with the 0.7.5 baseline.

The independent takeover branch carries these receipts forward unchanged from
PR #10. Their recorded source commit remains authoritative: they are evidence
from that earlier run, not newly measured results for the takeover branch.
The original correction commits retain their author and cherry-pick provenance.

- [`general-v3-2026-09-17-agent-path-smoke.json`](general-v3-2026-09-17-agent-path-smoke.json)
  records real CLI subprocesses through the benchmark adapter: fast returns
  paths without snippets; context and focused deep return `MinimumNArgs` and
  `ExactArgs` source evidence from the pinned Cobra `args.go`. This checks source
  snippets, not full parsed-document extraction. Deep may still use embeddings.
- [`general-v3-2026-09-17-cobra.json`](general-v3-2026-09-17-cobra.json)
  is a schema-v3, fresh-reset, integrity-checked Cobra receipt: 10 tasks, three
  paired trials, 60 policy observations, exact `tiktoken:cl100k_base` counting,
  and all compact rows retained. Both policies completed 30/30 observations.
  The source and quality gates pass, but `claim_status` remains `insufficient`:
  one repository and 10 unique tasks do not satisfy the general sample gate.
- [`general-v3-2026-09-17-cobra-gate.json`](general-v3-2026-09-17-cobra-gate.json)
  records exit 1 from applying the workflow's unchanged General thresholds to
  this partial receipt. Besides the insufficient sample, the context-reduction
  confidence-interval lower bound is 0.905, below the required 1.0. No general
  context-efficiency or latency claim is made from this result.
- [`general-v3-2026-09-17-capacity.json`](general-v3-2026-09-17-capacity.json)
  preserves the **failed** capacity preflight. Its own capacity-report schema
  remains v1; it consumes the corrected schema-v3 Cobra receipt.

Cobra's fresh index took 321.788 seconds. The unchanged capacity model projects
React at 46,841.554 seconds, Django at 37,175.232 seconds, and Spring Framework
at 29,671.489 seconds, each above the 18,000-second per-index limit. These are
**indexing projections, not measured query latencies**. The remaining five
fresh indexes were not started, as required by the capacity-first workflow.
No threshold was relaxed, fixture shortened, or old receipt mixed into this run.

The accelerated workflow was dispatched on the same source commit:
[run 35266809118](https://github.com/danielchen26/skylakegrep/actions/runs/35266809118).
At publication, its `capacity-cobra` job was queued without an assigned runner.
Completing the six-repository receipt requires an available runner labeled
`self-hosted, skygrep-benchmark` with local Ollama, `bge-m3`, and `rg`, passing
the existing capacity gate. Re-run all repository receipts on that runner
environment before merging them; the local partial receipt is not a substitute.
If this dispatch has expired, dispatch `general-benchmark.yml` from the
independent takeover branch, `agent/general-v3-latency-takeover`. Keep the
takeover PR in draft and follow-up #26 item 1 open until the full merged receipt
and its quality/latency gate are available. PR #10 remains the author's
predecessor; this takeover does not close it or rewrite its branch.

### Reproduce the local capacity decision

```bash
python -m pip install -e ".[benchmark]"
python benchmarks/validate_public_fixtures.py \
  --oss-root /tmp/skygrep-general-v3-repos --prepare
python benchmarks/universal_closed_loop_benchmark.py \
  --repo cobra --oss-root /tmp/skygrep-general-v3-repos --prepare \
  --refresh-index --reset-index --index-timeout 18000 \
  --trials 3 --tokenizer tiktoken --report /tmp/general-cobra.json
python benchmarks/general_capacity_preflight.py \
  --cobra-receipt /tmp/general-cobra.json \
  --oss-root /tmp/skygrep-general-v3-repos --prepare \
  --max-index-seconds 18000 --require-capacity \
  --report /tmp/general-capacity.json
```

The last command exits 2 when capacity is insufficient. Preserve that receipt;
do not bypass the gate to present an incomplete run as a general result.

### Reproduce the agent-path smoke

After indexing Cobra, run from its pinned checkout with the same installed
skylakegrep source:

```bash
skygrep search --agent-fast --top 8 --no-auto-index "args.go"
skygrep search --agent-context --top 8 --no-auto-index \
  --include args.go "MinimumNArgs ExactArgs"
skygrep search --agent-mode deep --no-llm-router --no-cascade \
  --top 8 --no-auto-index --include args.go "MinimumNArgs ExactArgs"
```

Require exit 0 and `args.go` in each JSON result. Fast must omit `snippet`;
context and deep snippets must contain both source identifiers.

## General Benchmark v2 — 2026-08-15

- [`general-v2-2026-08-15.json`](general-v2-2026-08-15.json) is the complete
  six-repository, 60-task, three-trial receipt. Its benchmark source is clean
  commit `e47e7f7b100bd1fcf30f28ea509703a1d2d1f17a`.
- [`general-v2-2026-08-15-cobra-capacity.json`](general-v2-2026-08-15-cobra-capacity.json)
  is the independent fresh Cobra reference run used by the capacity gate.
- [`general-v2-2026-08-15-capacity.json`](general-v2-2026-08-15-capacity.json)
  projects the six fresh-index workloads from that reference run. It is a
  runner-capacity receipt, not a retrieval-efficiency claim.

The reportable headline is a **17.982× median reduction in returned tool-context
tokens** on the 53 / 60 unique tasks where both policies met the same quality
floor. The repository-aware 95% hierarchical-bootstrap interval is
5.202×–94.127×. See [`../../docs/general-performance.md`](../../docs/general-performance.md)
for the full interpretation and important tool-call, latency, and scope
boundaries.

Do not hand-edit receipt JSON. Reproduce it with the commands in the General
Benchmark v2 methodology, run `scripts/privacy_release_scan.py`, and publish a
new dated receipt instead.
