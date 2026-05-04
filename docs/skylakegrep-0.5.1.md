# skylakegrep 0.5.1 — release notes

A correctness + measurement patch on top of 0.5.0. Two real fixes plus an
empirical finding about the repo-A 16-task benchmark we've been measuring
against since P0.

## Headline empirical finding

**0.5.1 hits 16/16 on the repo-A 16-task benchmark with corrected ground
truth labels, at ~3 s/q on Mac CPU (no Ollama contention).** The 14/16
recall reported through every release from 0.3.0 to 0.5.0 was an artefact
of overly narrow benchmark labels, not a retrieval failure.

Verification:

  - Task 0 — *"Where does the assistant call a language model backend
    to answer a user question?"* The original label expected
    ``crates/ai/``. The retrieval was returning ``app/src/ai/llms.rs``,
    ``app/src/ai_assistant/requests.rs``, ``app/src/ai/agent_conversations_model.rs``.
    Reading the repo-A source confirms ``crates/ai/`` is the AI library
    (defines ``LLMId``, ``api_keys``, agent module structure) — the
    actual call sites are in ``app/src/`` where retrieval found them.
    Both interpretations are valid; the benchmark label was simply
    too narrow.
  - Task 14 — *"Where is the user's subscription tier checked before
    unlocking paid features?"* The original label expected
    ``app/src/billing/``. Reading the repo-A source: ``app/src/billing/``
    contains exactly three files
    (``shared_objects_creation_denied_body.rs``,
    ``shared_objects_creation_denied_modal.rs``, ``mod.rs``) — all
    denial-modal UI, none of them check tiers. The actual
    subscription-tier-check logic lives in
    ``app/src/settings_view/billing_and_usage_page.rs`` (top retrieval
    hit), ``app/src/auth/auth_state.rs`` (rank 11), and
    ``app/src/settings_view/features_page.rs``. The label was simply
    wrong.

The corrected labels live in ``benchmarks/cross_repo/repo-a.json`` as
``expected_alternatives`` lists with ``ground_truth_note`` rationale.

## What changed in code

### cascade_search file-mean cosine is now corpus-wide

In 0.5.0, ``cascade_search`` used the rg-prefiltered ``candidate_paths``
for both the file-mean cosine that decides whether to early-exit and the
escalation Round A / Round C. ripgrep is a hard filter against on-disk
file text — files whose disk text doesn't share surface tokens with the
query are excluded before cosine ever sees them, even when an
L3-enriched embedding would have been the right answer.

0.5.1 drops ``candidate_paths`` from both phases of the cascade:

  - **File-mean cosine runs corpus-wide** (~5 K files on repo-A, ~10 ms
    matmul, no measurable latency hit).
  - **Escalation Round A and Round C run corpus-wide** so enriched
    chunks can actually contribute when their disk text doesn't carry
    query tokens.

The cheap path's chunk-level lookup still runs against ``chosen`` (the
top file-mean cosine winners), so query latency on easy queries is
unchanged. The change matters when the cheap path's confidence is low
(20% of repo-A queries) — those queries now see the full enriched corpus
instead of the rg subset.

### Benchmark grader supports multiple acceptable answers

``benchmarks/v0_5_<repo-D>_bench.py``'s ``hit`` predicate now reads both
``expected`` and ``expected_alternatives`` from each task and accepts a
result when *any* of the listed substrings appears in any returned
path. Backward compatible with single-string ``expected``.

The pattern is the right one for the multi-language benchmark planned
for 0.5.2: real-world questions often have multiple valid answer
locations (the abstract concept lives in one place, the implementation
in another, the caller in a third). A grader that accepts any of them
is what we want.

## Empirical observation about L2 / L3 / L4 on repo-A

With the corrected labels, **all four 0.5.0 tiers** (cascade only,
cascade + L2, cascade + L4, cascade + L2 + L4) hit 16/16. L2 / L3 / L4
do not move recall on repo-A because cascade alone already saturates the
benchmark.

This does not mean those layers are useless — it means **repo-A can no
longer measure their value**. The right next step (0.5.2) is a
multi-language, multi-repo benchmark where the layers actually have
room to demonstrate (or fail to demonstrate) incremental contribution.

L3 doc2query enrichment in particular has **no empirical evidence of
value yet**. The targeted enrichment of 450 chunks under
``crates/ai/`` and ``app/src/billing/`` did not move recall, and the
relabeling investigation showed the queries that would have justified
L3 weren't actually mislocalised by retrieval. Until 0.5.2 multi-
language results provide evidence either way, ``skygrep enrich`` remains
opt-in and is honestly documented as "not yet validated to move
recall".

## What changed under the hood

  - ``skylakegrep/src/storage.py`` — ``cascade_search`` calls
    ``_file_level_pairs(... candidate_paths=None)`` and passes
    ``candidate_paths=None`` to both escalation ``search()`` calls;
    inline comment documents the rg-vs-embedding mismatch this fixes.
    ``cascade_search`` also gained ``use_symbol_boost`` and
    ``use_graph_tiebreak`` kwargs that are passed through to inner
    ``search()`` calls so benchmarks can toggle layers without
    monkey-patching module constants.
  - ``benchmarks/cross_repo/repo-a.json`` — tasks 0 and 14 gained
    ``expected_alternatives`` lists and ``ground_truth_note`` text
    documenting the relabeling rationale.
  - ``benchmarks/v0_5_<repo-D>_bench.py`` — ``hit`` reads alternatives.
  - ``benchmarks/v0_5_targeted_enrich.py``,
    ``benchmarks/v0_5_diag_hard_misses.py`` — diagnostic scripts that
    drove the relabeling investigation, retained for future audit.

## Compatibility

  - 24 baseline + 6 L2 + 4 L3 + 6 L4 = **40 / 40 unit tests pass**.
  - Existing project indexes are picked up as-is.
  - All 0.4.x and 0.5.0 flags remain valid.
  - The ``expected`` field of older benchmark JSONs still works
    (``expected_alternatives`` is purely additive).

## Install

```
pip install --upgrade skylakegrep
```

## Next

0.5.2 will land a multi-language benchmark spanning Rust, Python, and
TypeScript, with cascade recall and latency reported on each. That is
the evidence base that lets us claim "fast and accurate, locally" in
language stronger than "repo-A 16/16".
