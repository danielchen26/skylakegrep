# skylakegrep 0.2.0 — release notes

This release reaches 30 / 30 (100 %) recall on the public OSS
benchmark across Django · React · Tokio while keeping the
`rg`-comparable token-reduction headline (60×–770× less context).
The accuracy gain comes from a substrate upgrade and a content-
agnostic rework of the retrieval graph, not from new heuristics.

> **License:** PolyForm Noncommercial 1.0.0. Personal / academic /
> research / hobby use is fully permitted. Commercial use requires
> a separate license — contact the maintainers.

## Headline numbers

| Repo | LOC ≈ | skygrep recall | Latency | Token reduction vs `rg` |
| --- | :-: | :-: | :-: | :-: |
| **Django** (Python) | 524 K | **10 / 10** | 10.13 s/q | **703 ×** |
| **Tokio** (Rust)    | 80 K  | **10 / 10** | 21.91 s/q | **61 ×**  |
| **React** (JS+TS)   | 270 K | **10 / 10** | 11.71 s/q | **773 ×** |
| **Aggregate**       |       | **30 / 30 (100 %)** | — | **60×–770×** |

vs `0.1.0` baseline (mxbai-embed-large substrate):

| | 0.1.0 | 0.2.0 | Δ |
| --- | :-: | :-: | :-: |
| Aggregate recall | 28 / 30 | **30 / 30** | **+2 (React 8/10 → 10/10)** |
| Three-repo avg latency | ~17 s/q | **~14.6 s/q** | **−14 %** |

The two original React misses (`react-007` test-fixture path bias,
`react-010` devtools-vs-reconciler confusion) are now resolved.
`docs/parity-benchmarks.md` records both the original failure modes
and the resolution as the engineering record, not erased.

## What changed

### 1. Embedding substrate: `mxbai-embed-large` → `bge-m3` (default)

`bge-m3` (1024-d, multilingual, symmetric XLM-RoBERTa) replaces
`mxbai-embed-large` as the default embedder. The substrate change is
the single largest accuracy contributor — the canonical reconciler
and source files now land in the cosine top-K pool, where any
post-hoc graph prior could not have surfaced them before.

**Breaking change for existing indexes.** The vector space has
changed; existing `~/.skylakegrep/index.db` indexes must be rebuilt:

```bash
ollama pull bge-m3
skygrep index <repo> --reset
```

`mxbai-embed-large` is still supported via `OLLAMA_EMBED_MODEL=mxbai-embed-large`.
Asymmetric models (e5-large, bge-large) get correct query/passage
prefixes via the new `EMBED_PREFIXES` map in `skylakegrep.src.config`.

### 2. Content-agnostic reference-graph registry

`skylakegrep.src.code_graph` is now a 75-line back-compat facade
over `skylakegrep.src.reference_graph` plus
`skylakegrep.src.extractors.{code,markdown}`. The abstraction is
"A references B", not "imports / use / require" — new content types
plug in via one line:

```python
from skylakegrep.src.reference_graph import register_extractor
register_extractor("yaml", [".yaml", ".yml"], my_yaml_extractor)
```

`markdown` extractor is shipped in 0.2.0 and parses `[text](target)`,
`![img](src)`, and `[[wiki]]` links with relative-path resolution
and Obsidian conventions. All legacy `code_graph` imports
(`build_export_graph`, `populate_graph_table`, `_rust_edges`,
`_python_edges`, `_ts_edges`) keep working unchanged.

### 3. σ-adaptive cascade gap

The cascade early-exit threshold is now derived from the top-K
cosine standard deviation rather than a hard-coded magic number:

```python
tau_eff = max(CASCADE_TAU_FLOOR, CASCADE_K_SIGMA * sigma_topK)
```

This is a minimal MacKay/Williams Bayesian-evidence framing —
high-σ queries (cosine has clearly separated candidates) early-exit
without the rerank tier; low-σ queries (cosine is uncertain) escalate.
The threshold auto-recalibrates when the embedder swaps. New
telemetry field `tau_mode` reports `"static"` or `"adaptive"`.

Tunable via `SKYGREP_CASCADE_TAU_FLOOR` (default 0.005) and
`SKYGREP_CASCADE_K_SIGMA` (default 1.0; 0 disables adaptive).

### 4. Universal non-canonical-path filter

The `_NON_CANONICAL_PATH_PATTERNS` list expanded from 11 patterns
(mostly `__tests__/`) to 24 universal aux-path conventions:
`/fixtures/`, `/__fixtures__/`, `/examples/`, `/sample(s)/`,
`/demos/`, `/vendor/`, `/third_party/`, `/node_modules/`, `/dist/`,
`/build/`, `/target/`, `/out/`, `.development.js`, `.development.ts`,
`.production.js`, `.production.min.js`, `.min.js`. A path-matching
bug (relative paths never matched leading-slash patterns) is fixed.

This is a structural prior, not a language-specific rule — it
applies to any corpus that follows similar conventions (markdown
notes with `/drafts/`, knowledge graphs with `/archive/`, etc.).

### 5. Symbol-as-retriever channel (opt-in, internal)

A new `skylakegrep.src.symbol_channel` module exposes
`symbol_channel_search` and `multi_channel_search` (cosine + symbol
fused via Reciprocal Rank Fusion, k=60 per Cormack et al. 2009).
**It is not wired into the default CLI link** — on the `bge-m3`
substrate the cosine-only baseline already reaches 30/30 on the
public bench, and fusion regresses on `react-010` because symbol
matches pull less-relevant same-named files into the pool.

The channel is shipped as an experimental retrieval primitive;
exposing it through the CLI behind an auto-router that decides
per-query whether to consult the symbol channel is tracked as a
0.3.0 follow-up. The decision will be evidence-driven on an
expanded benchmark with symbol-heavy queries (`find useState
implementation`, `where is tokio::spawn defined`, etc.).

## Compatibility

- **Python**: ≥ 3.9 (unchanged from 0.1.0)
- **Ollama**: default embedder is now `bge-m3`; `mxbai-embed-large`
  remains supported via env var
- **Existing indexes**: must be rebuilt (`--reset`) — vector space changed
- **Existing imports**: all `code_graph` public symbols re-exported from
  the new module location; no breakage expected

## Reproduce the bench

```bash
git clone --depth=1 https://github.com/django/django   /tmp/oss-bench/django
git clone --depth=1 https://github.com/facebook/react  /tmp/oss-bench/react
git clone --depth=1 https://github.com/tokio-rs/tokio  /tmp/oss-bench/tokio

ollama pull bge-m3
.venv/bin/python benchmarks/public_oss_bench.py
```

See `docs/parity-benchmarks.md` for per-task analysis, the original
failure modes, and the methodology.

## Known follow-ups (not in 0.2.0)

- Re-render `docs/assets/benchmark.svg` and `docs/assets/schema.svg`
  to reflect the new defaults (visual assets, cosmetic)
- Re-run the self-test bench (`benchmarks/agent_context_benchmark.py`)
  on `bge-m3` and update `docs/token-benchmarking.md` top-k 5 row
- Symbol-channel auto-router behind `SKYGREP_SYMBOL_CHANNEL=auto|on|off`,
  validated against an expanded symbol-heavy benchmark — slated for 0.3.0
