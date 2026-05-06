# Proactive umbrella framework — the conceptual model

> Authoritative since 0.5.6. Whenever query routing is being changed
> (cascade gate, lazy seed selection, cross-folder search, filename
> enhancers), the change MUST conform to the model on this page.

## Two layers, parallel

```
              Query
                │
                ▼
       ┌────────┴────────┐
       │                 │
     cascade        proactive umbrella
                        │
                        ├── lazy_cwd       (cold-start, embed cwd seeds)
                        ├── lazy_cross_folder (embed sibling-dir seeds)
                        ├── filename_extend   (~/Downloads etc filename glob)
                        └── (future speculative tiers)
```

`cascade` is the path that **assumes the user is in the right project
and the index is built**. It's optimised for that case (σ-adaptive
cosine, optionally cross-encoder rerank).

`proactive umbrella` is the path that **does not** assume that — it
fans out into multiple parallel subprocesses, each exploring a
different "the answer might live here" hypothesis:

- `lazy_cwd` — embed-on-demand semantic search inside the current
  cwd when no index has been built yet (0.5.x cold-start lazy).
- `lazy_cross_folder` — embed-on-demand semantic search across
  the user-curated sibling folders (`SKYGREP_PROACTIVE_DIRS`) when
  the cwd answer might not be in cwd at all.
- `filename_extend` — `find -iname` over `~/Downloads`,
  `~/Desktop`, `~/Documents` (or `SKYGREP_PROACTIVE_DIRS`) for
  file-name queries that point to data files (PDFs, docx, etc.)
  outside any code repo.

## Lazy IS proactive

In the user's vocabulary:

- **"proactive"** describes the *behaviour* — speculative parallel
  exploration when we don't know whether the cwd / index assumption
  holds.
- **"lazy" / "lazy auto-trigger" / "lazy index"** describes the
  *technique* — embed only what's needed, on demand, instead of
  paying for an upfront full-corpus index.

`lazy_cwd` and `lazy_cross_folder` are subprocesses USING the lazy
technique. `filename_extend` is a different subprocess (pure
filename glob, no embedding) but lives at the SAME LAYER.

The two-module split in code (`skylakegrep/src/proactive.py` for
`filename_extend`, `skylakegrep/src/lazy_indexer.py` for
lazy_cwd / lazy_cross_folder) is an implementation accident, not
a layer boundary. Always reason at the umbrella-layer level.

## Why parallel and not sequential

When a user types `skygrep "<query>"` from any cwd, the system
cannot know whether:

1. The cwd is the right project (vs the user wandered in by
   mistake — <repo-A> vs `~/Downloads`).
2. The index covers what the user is asking about (cwd may be
   indexed but the answer is a PDF in `~/Downloads`).
3. The query is a code concept (cascade-friendly) or a file-name
   question (filename_extend-friendly) or about a sibling repo
   (cross-folder-friendly).

The historical mistake (pre-0.5.6) was to treat proactive as
"cascade fallback" and only fire it when cascade returned weak.
That serialised the chain: filename + rg → cascade (100 ms ~ 60 s)
→ cross-folder (5–30 s) → proactive enhancers (≤ 1 s) → render.
On vocabulary-mismatch queries the chain ran 60–120 seconds end
to end, with the user staring at a blank prompt.

The 0.5.6 refactor moves the umbrella subprocess to fire **at
t = 0**, in parallel with cascade. Each tier streams its results
as soon as it's ready. The user always sees activity within
≤ 1 s and gets the first viable answer from whichever tier
completed first.

## Required UX

1. **First answer in ≤ 1–3 s.** If `filename_extend` finds the
   answer in `~/Downloads` in 100 ms, the user sees it in 100 ms,
   regardless of what cascade is doing.
2. **Streaming with route labels.** Each printed result block has
   a header like `▾ filename_extend (~/Downloads)` or
   `▾ cascade (cwd · σ-gap=0.05, conf=high)` so the user knows
   which tier produced it and can judge quality accordingly.
3. **"Still searching" status.** While slow tiers are working,
   stderr shows lines like `↻ cascade running… (σ-adaptive
   rerank, est. 30 s)` so the user knows the system is actively
   exploring.
4. **Hard timeouts on every slow tier.** cascade ≤ 30 s,
   cross-folder ≤ 8 s, filename_extend ≤ 1 s. After timeout,
   stderr says `(tier X timed out — top-K above is the answer
   from the other tiers)`.
5. **Quality indicator per block.** A user reading the
   `filename_extend` block knows it's a fast filename glob (no
   semantic understanding); a user reading the cascade block
   knows it's σ-validated; a user reading lazy_cross_folder
   knows it's "approximate semantic, partial recall." This lets
   the user judge whether to wait for cascade to finish or trust
   the early answer.

## Forbidden patterns

- ❌ Treating any proactive subprocess as "fallback after
  cascade." All speculative tiers run alongside cascade.
- ❌ Designing a "chain" where tier B waits on tier A. Each tier
  runs independently in its own thread.
- ❌ Single-block render at the end. Always stream.
- ❌ Proactive `should_fire` predicates that depend on cascade
  results. Fire ALL umbrella subprocesses unconditionally; let
  the merge step decide which ones contributed useful answers.
- ❌ Calling proactive "the filename_extend module" when
  discussing the umbrella concept. The module is the
  implementation; the umbrella is the conceptual layer.

## Receipts (why this page exists)

- 0.5.0–0.5.4: lazy was treated as "cold-start fallback" — only
  ran when cwd index was missing. Cross-folder lazy was treated
  as "warm-cascade fallback" — only ran when cascade gap was
  small. Both were sequential after cascade.
- 0.5.5 user test: query `'我有没有跟 "tax-2024" 有关的文件？'` on
  the indexed `<repo-A>` repo took **12 minutes 50 seconds** wall
  clock. Cascade ran 99.7 s (escalated to rerank because zero
  semantic match), cross-folder timed out at 8 s, then proactive
  `filename_extend` finally fired and **found the answer in
  ~100 ms**: 4 TAX-2024 PDFs in `~/Downloads`. The answer existed
  the whole time but was hidden behind sequential cascade work.
- 0.5.6 fixes this by firing all umbrella subprocesses at t = 0
  in a ThreadPool alongside cascade.

The user's exact framing (2026-05-06):

> "lazy 不就是 proactive 的行为吗?" (lazy IS proactive behaviour)
>
> "在 user 看来应该是我们一直是在背后不停的寻找
>  并且但凡得出答案就直接就显示" (from the user's POV, the
>  system should appear to be continuously searching in the
>  background and stream any answer the moment it has one)

## Related

- `skylakegrep/src/cli.py` — the dispatcher that wires cascade +
  umbrella subprocesses in parallel.
- `skylakegrep/src/proactive.py` — historic name; contains
  `filename_extend` and the enhancer framework.
- `skylakegrep/src/lazy_indexer.py` — `lazy_explore_cold_start`
  and `lazy_explore_cross_folder`.
- `docs/skylakegrep-0.5.6.md` — the release that wired the
  parallel architecture.
- `docs/skylakegrep-0.5.5.md` — pre-refactor state with
  receipts of the silent-30s problem.
