# skylakegrep 0.5.15 — first-class agent presets and release gates

0.5.15 turns the 0.5.14 agent playbook into explicit CLI and release
contracts. The search engine is the same generic router; this release
removes flag-bundle ambiguity for LLM callers, makes setup freshness
auditable, and replaces the broken token-based PyPI workflow with
Trusted Publishing.

## What changed

- `--agent-fast` is now the official path-anchor preset for LLM callers.
  It expands to JSON output, no snippets, top-10 candidates, and no rerank.
- `--agent-context` is now the official first evidence preset. It expands
  to JSON output, standard snippets, and no rerank.
- `--agent-daemon` makes daemon-first repeated calls explicit. It uses
  `SKYGREP_DAEMON_URL` when set, otherwise `http://127.0.0.1:7878`, and
  falls back in-process if no daemon is running.
- `skygrep setup --check` reports whether managed Claude Code, Codex,
  OpenCode, Gemini CLI, and Cursor snippets are current, stale, missing,
  or broken without modifying user files.
- Managed setup snippets now carry an internal guidance version and teach
  the shorter agent presets first, while keeping the explicit long-form
  commands documented for auditability.
- `benchmarks/closed_loop_regression_gate.py` turns saved closed-loop
  benchmark JSON into a machine-checkable release gate.
- `.github/workflows/release.yml` now uses PyPI Trusted Publishing
  (OIDC) instead of `PYPI_API_TOKEN`.

## Agent command contract

Use these commands when another LLM will consume the result:

```bash
skygrep --agent-fast "where is token refresh implemented?"
skygrep --agent-context --include "src/**" "what does token refresh do?"
skygrep serve --port 7878
skygrep --agent-daemon --agent-context "what does token refresh do?"
```

The explicit long-form equivalents still work:

```bash
skygrep --json --no-content --top 10 --no-rerank "<query>"
skygrep --json --content --detail standard --no-rerank "<query>"
```

## Setup freshness

Existing managed setup blocks are still refreshed automatically on normal
interactive use, and `skygrep setup` still updates only the managed
BEGIN/END block while preserving user-authored instructions. The new
check mode gives users and CI a non-mutating audit command:

```bash
skygrep setup --check
```

## Benchmark gate

The universal benchmark remains the real measurement path:

```bash
python benchmarks/universal_closed_loop_benchmark.py \
  --repo self --repo django --repo react --repo tokio --summary-only \
  > /tmp/skygrep-closed-loop.json
```

The new release gate checks the saved report:

```bash
python benchmarks/closed_loop_regression_gate.py /tmp/skygrep-closed-loop.json
```

Default gate thresholds require skygrep-first to preserve at least 90 %
path coverage, 90 % evidence coverage, 85 % sufficiency, 2× context
reduction, no estimated agent elapsed regression, and no work-quality
per-minute regression. Release runs can tighten those thresholds when the
benchmark fixture is fully prepared.

## Publishing

The GitHub release workflow now expects PyPI Trusted Publishing with:

- owner: `danielchen26`
- repository: `skylakegrep`
- workflow: `release.yml`
- environment: `pypi`

That removes the long-lived `PYPI_API_TOKEN` failure mode.

## Verification

- Targeted CLI/setup/benchmark tests cover the new agent presets, daemon
  expansion, setup status checks, and regression gate.
- Full test suite and privacy release scans were run before publishing.
- Public examples in this release use only generic placeholder questions.
  No local private path, filename, screenshot, or personal benchmark content
  is included.
