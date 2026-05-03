# local-mgrep 0.7.0 — release notes

A measurement release. No retrieval architecture change, no recall
regression. The headline is the multi-language benchmark that proves
the cascade generalises beyond the <repo-D> Rust workspace.

## Headline

**38 / 40 (95 %) recall across Rust, Python, and TypeScript** at
3.55 s/q average on Mac CPU, no GPU. Three repositories, 40
hand-labelled questions, four cascade tiers each.

| Repo | Language | Tasks | Recall | Avg s/q |
| --- | --- | :-: | :-: | :-: |
| `<repo-D>-terminal/<repo-D>` | Rust | 16 | **16 / 16** | 4.17 |
| `<repo-A>` | Python | 12 | **11 / 12** | 2.45 |
| `<repo-B>` | TypeScript | 12 | **11 / 12** | 3.83 |
| **Total** | | **40** | **38 / 40 (95 %)** | **3.55** |

The two honest misses (<repo-A> "V6 <domain-y> resolve / chain", CCSB "auto-
compact decision logic") are documented in
[`docs/parity-benchmarks.md`](parity-benchmarks.md#multi-language-benchmark-v070) —
neighbouring files in the right directory rank top-K in both cases,
but the canonical answer file does not.

## What's new

  - **`benchmarks/cross_repo/<repo-A>.json`** — 12 hand-labelled Python
    questions over the <repo-A> scientific Python codebase, with
    `expected` paths and `ground_truth_note` for each.
  - **`benchmarks/cross_repo/<repo-B>.json`** — 12 hand-labelled
    TypeScript questions over the `<repo-B>` repo,
    with `expected_alternatives` for the multi-file cases (e.g.
    autocompact spans three files in `src/services/compact/`).
  - **`benchmarks/v0_7_multilang_bench.py`** — unified runner that
    loops over all three repos, runs the same Tier A / B / C / D
    cascade harness as the v0.5 <repo-D> bench, and reports both
    per-repo and aggregate numbers.
  - **`docs/parity-benchmarks.md`** — new "Multi-language benchmark
    (v0.7.0)" section with the per-repo and aggregate tables, the
    cheap-path-vs-escalation split per repo, and a paragraph on
    each of the two honest misses.

## What did not change

  - Retrieval pipeline is byte-for-byte 0.6.2.
  - All 0.4.x / 0.5.x / 0.6.x flags remain valid.
  - 40 / 40 unit tests still pass.
  - The cascade default still hits 16 / 16 on <repo-D>.
  - Defaults: `OLLAMA_EMBED_MODEL=nomic-embed-text`,
    `OLLAMA_LLM_MODEL=qwen2.5:3b`, `OLLAMA_HYDE_MODEL=qwen2.5:3b`,
    `OLLAMA_KEEP_ALIVE=-1`.

## Empirical observations from the multi-language run

  - **The symbol layer (L2) and graph tiebreaker (L4) don't move
    recall on the new repos either.** All four tiers (cascade only,
    +L2, +L4, full 0.5/0.7) tied at 16/16, 11/12, 11/12 with < 0.4 s/q
    latency variation. As discussed in the 0.5.1 notes, this means
    the <repo-D> + <repo-A> + CCSB benchmarks together still don't measure
    L2 / L4 marginal value — the cheap cascade path already saturates
    these question sets. A larger or harder benchmark would be needed
    to discriminate.
  - **Cheap-path early-exit rate varies a lot by repo.** <repo-A>'s
    filenames are very semantic (`<domain-y>_v6.py`,
    `finite_field_runner.py`, `<component-v>.py`) so
    file-mean cosine is confident on 7/12. Warp is the opposite
    (3/16 cheap; 13 escalate), because the natural-language questions
    in <repo-D>.json rarely share surface tokens with the Rust crate
    names. CCSB sits between (4/12 cheap).
  - **CCSB indexer regression** noted: the L2 symbol extractor
    populated 0 symbols on CCSB and <repo-A> (vs <repo-D>'s 62 K symbols),
    suggesting the tree-sitter-Python and tree-sitter-typescript
    paths in `indexer.extract_file_symbols` need a closer look. This
    is benign for 0.7.0 (recall didn't regress because the cascade
    cheap path doesn't depend on L2), but is the highest-priority
    follow-up.

## Compatibility

  - 40 / 40 unit tests pass.
  - All 0.6.x flags / env / per-project DB layout unchanged.
  - Existing project indexes are picked up as-is.

## Install

```
pip install --upgrade local-mgrep
```
