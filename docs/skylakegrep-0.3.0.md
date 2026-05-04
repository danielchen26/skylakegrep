# skylakegrep 0.3.0 — release notes

This release adds **confidence-gated cascade retrieval** as a new
max-accurate tier, four reproducible probe scripts that document the
hypothesis-test path that led there, and three honest null-result findings
so future work doesn't re-litigate them.

## Headline change

`skygrep search --cascade` (opt-in) hits **14/16 recall on the repo-A 16-task
benchmark at 1.49 s/q on Mac CPU** — the same recall as the previous
max-accurate tier (`--rerank --hyde --rank-by file`, 21.8 s/q) at **14×
lower latency**.

| Tier | Command | Recall | Avg s/q |
| --- | --- | :-: | :-: |
| ultra-fast | `--cascade --cascade-tau 0.0` | 11/16 | 0.10 |
| **cascade default** ⭐ | `--cascade` (τ=0.015) | **14/16** | **1.49** |
| previous max | `--rerank --hyde --rank-by file` | 14/16 | 21.8 |

The cascade is opt-in — non-cascade defaults are unchanged. Existing
flags (`--rerank`, `--hyde`, `--rank-by`, `--multi-resolution`,
`--lexical-prefilter`, `--daemon-url`) all continue to work as before.

## Architecture: confidence-gated cascade

```
query
  │
  ├─ ripgrep -il -F   → candidate file set
  │
  ├─ file-mean cosine → top-N (path, score) pairs
  │     ├─ if (top1.score - top2.score) ≥ τ:
  │     │     return one chunk per file (cheap path, ~0.1 s)
  │     │
  │     └─ else (uncertain):
  │           ├─ Round A: cosine + file-rank
  │           ├─ Round C: HyDE + cosine + file-rank
  │           └─ score-preserving union, top-K
  │
  └─ result list
```

Cheap path runs on ~80% of repo-A queries at τ=0.015 (`exit% = 81%`); the
remaining ~20% pay the full LLM-augmented escalation. Aggregate latency is
dominated by the cheap branch.

### CLI flags

```
--cascade / --no-cascade            opt in to cascade retrieval (off by default)
--cascade-tau FLOAT                 confidence threshold (default 0.015)
```

The status line reports the cascade decision and gap so you can tune the
threshold for your corpus:

```
[Search completed in 0.103s; cascade=cheap (gap=0.0241 τ=0.0150)]
[Search completed in 2.311s; cascade=escalated (gap=0.0046 τ=0.0150)]
```

## Recall ceiling and remaining misses

Two repo-A queries still miss at 14/16 across every tested configuration:

- task 0: *"Where does the assistant call a language model backend to answer
  a user question?"* → `crates/ai/`
- task 14: *"Where is the user's subscription tier checked before unlocking
  paid features?"* → `app/src/billing/`

These are hard semantic-disambiguation cases where the canonical file's
surface vocabulary doesn't overlap the question's. Plain HyDE,
multi-variant HyDE union, and LLM filename arbitration all fail on them.

## Null results (do not re-litigate)

Three orthogonal directions tested in this release. Each has a probe
script under `benchmarks/` you can run yourself.

### LLM filename arbitration (`benchmarks/llm_arbitration_probe.py`)

Feed top-N rg paths to a small LLM (qwen2.5:3b), ask which 5 are most
likely, then run cosine on those 5 only.

| Variant | Recall | Avg s/q |
| --- | :-: | :-: |
| LFA-only (20→5) | 9/16 | 0.91 |
| LFA-only (30→5) | 10/16 | 1.17 |
| LFA-rerank (20→5) | 9/16 | 1.05 |
| LFA-rerank (30→5) | 10/16 | 1.10 |

All worse than B-only baseline (11/16). qwen2.5:3b cannot reliably
localise from filenames alone. Bigger LLMs would likely do better but add
prohibitive latency on local hardware.

### Code-graph centrality (`benchmarks/code_graph_probe.py`)

Parse Rust `use crate::…` and `mod` to build per-file in-degree; add
`α · log(1+indeg)` prior to file-cosine.

| α | Recall |
| :-: | :-: |
| 0.00 | 11/16 |
| 0.05 | 9/16 |
| 0.10 | 7/16 |
| 0.15 | 7/16 |
| 0.20+ | 6/16 |

Hub files (`crates/<repo-D>ui/src/lib.rs` with 1655 in-degree, `app/src/
terminal/mod.rs` with 245) systematically beat canonical leaves. The
centrality signal points the wrong direction for "where is X implemented"
queries.

### Multi-HyDE union (`benchmarks/multi_hyde_probe.py`)

Three independent HyDE variants per query (sdk-call, ident-list,
crate-path), search each, union top-K.

| Strategy | Recall | Avg s/q |
| --- | :-: | :-: |
| single (sdk-call) | 11/16 | 1.49 |
| single (ident-list) | 11/16 | 0.47 |
| union (3 variants) | 14/16 | 3.75 |

Saturates at the same 14/16 ceiling as the cascade, at 2.5× the latency.
The 2 hard misses survive every variant.

## What changed

- `skylakegrep/src/storage.py`: new `cascade_search()` and
  `_file_level_pairs()` helpers; `CASCADE_DEFAULT_TAU = 0.015`.
- `skylakegrep/src/cli.py`: new `--cascade` and `--cascade-tau` flags;
  status line reports the cascade decision.
- `docs/parity-benchmarks.md`: new headline-table row 3f and a new
  "Confidence-gated cascade" section with the full τ sweep.
- `docs/roadmap.md`: P4-CC entered as DONE; P4-LFA, P4-CGC, P4-MH
  documented as ABANDONED with the empirical reasons.
- `benchmarks/`: six new reproducible probe scripts —
  `cascade_probe.py`, `cascade_production_bench.py`, `code_graph_probe.py`,
  `llm_arbitration_probe.py`, `multi_hyde_probe.py`,
  `multi_round_probe.py`.

## Compatibility

- Default behaviour is unchanged. The cascade is opt-in.
- All 14 unit tests pass. The non-cascade search path is byte-for-byte
  identical to 0.2.0.
- No new required dependencies. Cascade reuses the existing Ollama
  embedding + LLM endpoints already wired for `--hyde`.
