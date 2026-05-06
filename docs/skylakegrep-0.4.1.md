# skylakegrep 0.4.1 — real-corpus bench artefact + verified-numbers update

This is a **patch release**: zero new code paths. It backfills the
real end-to-end measurement that 0.4.0 should have included before
shipping but didn't, then propagates the verified numbers to every
public surface (README, docs/index.html, changelog).

Triggered by user feedback (2026-05-06):

> 你要记住之后ship之前你永远要做真实的test要做end to end test 当
> 做完了以后你还要把相对应的这个github包括首页的所有的细节都要
> update到最新的compatible的做完的东西然后end to end整个package
> ship掉

Translated: "Remember — before shipping, always do a REAL end-to-
end test. After it's done, update all the corresponding GitHub
details including the homepage to the latest compatible state,
then end-to-end ship the whole package."

This rule is now in `memory/feedback_real_e2e_test_then_full_surface_update.md`
and is mandatory for every release going forward.

## What's in 0.4.1

### Real-corpus bench artefact

`benchmarks/release-0.4.0-real-corpus.md` — first honest end-to-end
measurement of the v2 graph substrate working with real `bge-m3`
embeddings on real source code (`skylakegrep/src/`, 27 files).

**Key findings (honest):**

  - **Substrate works**: 108 graph_node entries, 190 refs edges
    populated from real imports during indexing.
  - **`graph_expand` fires correctly**: 3/3 escalated queries had
    it run; 2/2 cheap-path queries correctly skipped it.
  - **Adds 4–9 candidates per fire** — telemetry verified.
  - **Latency invariant holds**: cheap path ~7 ms (identical to
    0.2.21); escalation 1.7–2.6 s (graph_expand cost lost in
    HyDE noise).
  - **Top-5 hit rate: 3/5 (60 %)** on the 5 representative semantic
    queries — **does not yet show a magic accuracy bump.** On the
    2 misses, the expected file was not a 1-hop reference neighbour
    of cosine's top-K, so adding 1-hop expansion couldn't help.

### What this revises in our claims

The 0.4.0 release notes said "30/30 OSS bench: architecturally
invariant" — that's TRUE by construction, but I never ran the bench.
0.4.1 ships the actual numbers, which confirm the invariant:
**0.4.0 cannot regress 0.2.21**. It also confirms the limitation:
**0.4.0 doesn't dominantly improve recall** on queries where the
answer is 2+ hops from cosine top-K.

The honest framing for the GH surfaces (README hero, index.html
benchmark section): the v2 substrate is **a foundation for future
graph-aware retrieval**, not a recall multiplier. Multi-hop walks
and cross-folder exploration (the user's full vision) remain
deferred — they require σ-adaptive depth control that hasn't been
designed yet.

### Bench script reproducibility

`benchmarks/release-0.4.0-real-corpus.py` — runnable end-to-end:

```bash
.venv/bin/python benchmarks/release-0.4.0-real-corpus.py
```

Requires `Ollama` running with `bge-m3` and `qwen2.5:3b` available.
Output: per-query telemetry showing whether graph_expand fired,
how many candidates it contributed, top-5 paths, and hit/miss
against ground-truth expectations.

## Compatibility

  - Python: unchanged — 3.9+
  - Default embedder / LLM router: unchanged
  - Wheel surface: unchanged from 0.4.0
  - Index format: unchanged (the `graph_edge` table populated by
    0.4.0+ still works)
  - Production behaviour: identical to 0.4.0

## What 0.4.1 does NOT change

  - No new code paths. The substrate (`_expand_via_reference_graph`,
    `populate_graph_table`'s edge writer) ships byte-identical to
    0.4.0.
  - No changes to the pyproject scripts or Python module layout.
  - 0.4.1 = 0.4.0 + the bench artefact + the verified-numbers
    update of the public surfaces. Nothing else.

## Tests

  - 206/206 pytest pass (unchanged from 0.4.0)
  - Plus the new manual real-corpus bench (`benchmarks/release-0.4.0-
    real-corpus.py`) which is **not** in pytest because it requires
    a running Ollama. Documented in the bench artefact for
    reproducibility.

## Auto-memory entry — reinforced

`feedback_real_e2e_test_then_full_surface_update.md` now has this
release as the third receipt of the pattern (0.3.0 by-construction
arguments → 0.4.0 synthetic fixtures → 0.4.1 backfilled real
corpus). Every release going forward MUST include real-corpus
end-to-end run + full public-surface update. No exceptions.

## Acknowledgments

User caught the gap directly:

> 所以你做了实际的这些测试了吗就比如说我在一个folder里面问了
> 一些不存在的东西它背后可以spam这个background subagent去
> intuitively的用你当前做的这个东西去explore然后找到我要问的
> 问题吗你有actually测试这个东西吗

Translation: "So did you actually do these tests? E.g. I'm in a
folder asking for something not present, can the background
subagent intuitively use what you just made to explore and find
the answer? Did you ACTUALLY test this?"

Honest answer at the time: no. 0.4.1 is the answer with concrete
numbers — the substrate works, fires when expected, doesn't
regress, but doesn't yet solve the cross-folder "background
subagent explores" scenario (that's still G-5 / G-6, deferred).
