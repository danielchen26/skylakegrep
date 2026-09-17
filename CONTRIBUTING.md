# Contributing to skylakegrep

## License of contributions

skylakegrep is Apache-2.0. Contributions are **inbound = outbound**: by
opening a pull request you license your contribution under Apache-2.0 on
the same terms, per Section 5 of the license. There is no CLA to sign
and no copyright assignment — you keep your copyright.

Sign off each commit to certify you have the right to submit it
([Developer Certificate of Origin 1.1](https://developercertificate.org)):

```bash
git commit -s -m "fix: ..."
```

That adds the trailer `Signed-off-by: Your Name <you@example.com>`.

Every new source file needs the SPDX header:

```python
# SPDX-License-Identifier: Apache-2.0
```

## What gets merged

This project has a hard rule: **claims must be measured, not asserted.**
See [`docs/PRINCIPLES.md`](docs/PRINCIPLES.md).

A pull request that changes retrieval behaviour, ranking, routing, or
latency must carry evidence:

- the tests you added or changed, and why they would fail on the old code
- for anything performance- or accuracy-adjacent, a benchmark run from
  `benchmarks/`, committed as a receipt under `benchmarks/reports/`

"Should be faster" without a receipt is not reviewable and will not be
merged.

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[rerank]'

python -m pytest -q tests/
```

Requires a running [Ollama](https://ollama.com) for anything that
actually embeds. Tests that need it skip themselves when it is absent.

## Scope

Two things are deliberately out of scope, and a PR implementing them
will be declined regardless of quality:

- **A cloud-hosted index.** This tool is local-first. That is the whole
  product thesis, not a limitation waiting to be fixed.
- **Sending file contents to a third-party API by default.** Any
  non-local path must be opt-in, explicit, and off unless the user turns
  it on.

## Reporting a security issue

Do not open a public issue. Email chentianchi@gmail.com with the
details and a reproduction.
