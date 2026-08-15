# Published benchmark receipts

This directory contains immutable, privacy-scanned benchmark receipts that
support public performance claims. A receipt records the benchmark source
commit, environment, exact public fixture commits, all success and failure
rows, and the aggregate gate result.

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
