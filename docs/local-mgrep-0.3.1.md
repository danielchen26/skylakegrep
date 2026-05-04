# local-mgrep 0.3.1 — release notes

Documentation patch on top of [0.3.0](local-mgrep-0.3.0.md). No behaviour
changes; the search and index code paths are byte-for-byte identical.

## What changed

- **README rewrite** — Overview now describes the four-stage pipeline
  (lexical prefilter → multi-resolution cosine → cascade decision →
  optional cross-encoder rerank). Quickstart shows `--cascade` and
  `mgrep serve`. A new "three retrieval tiers" table replaces the
  pre-cascade narrative. Full CLI options table now lists every flag
  introduced through P0–P4 (`--rerank`, `--rerank-pool`,
  `--rerank-model`, `--hyde`, `--multi-resolution`, `--file-top`,
  `--lexical-prefilter`, `--lexical-root`, `--lexical-min-candidates`,
  `--rank-by`, `--cascade`, `--cascade-tau`, `--daemon-url`).
- **Capability matrix** now records each capability's introducing
  version (cross-encoder rerank, HyDE, multi-resolution, lexical
  prefilter, file-rank, daemon mode, quantisation knobs, and the
  cascade are all flagged as 0.3.0).
- **Benchmark section** restructured into two subsections: the repo-A
  16-task cross-repo benchmark (the headline 14/16 @ 1.49 s/q cascade
  number) and the 30/30 self-test regression guard.
- **Releases section** added — links every version's GitHub Release and
  PyPI artifact so the project history is browseable from the README.
- **Hero SVG** version label bumped from `v0.2.0` to `v0.3.0`.
- **Architecture SVG** query-time lane updated: boxes 3–5 are now
  "rg prefilter", "Multi-res cosine", and "Rerank / cascade" so the
  diagram matches the actual 0.3.x flow.

## Why a patch release

`pyproject.toml` declares `readme = "README.md"`, so the PyPI long
description is whatever `README.md` looks like at build time. Without a
patch bump, the PyPI page for `local-mgrep` would keep displaying the
0.2.0-shaped overview and capability matrix even though the latest
shipped wheel (0.3.0) already implements the cascade. 0.3.1 ships the
new README so `https://pypi.org/project/local-mgrep/` matches the GitHub
README.

## Install

```
pip install --upgrade local-mgrep
```

## Compatibility

- Behaviour-compatible with 0.3.0; no flags added, removed, or renamed.
- All 14 unit tests pass.
- The cascade retrieval (`--cascade`) introduced in 0.3.0 is unchanged.
