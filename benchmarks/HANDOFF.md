# Handoff: the two experiments this network cannot run

Everything on this branch was measured on a machine where `huggingface.co` is
blocked by category (SASE gateway, `307` to a block page,
`reasoncode=CATEGORY_DENIED`). That block is itself one of the findings — it is
what disables `ck` entirely — but it also leaves exactly two questions
unanswered, and both need the same thing: **a machine that can reach
huggingface.co.** One trip, two answers.

## What is already settled

Six pinned repositories, 180 paired tasks, 153,022 chunks, `trials 3`, exact
`tiktoken:cl100k_base`. The run reproduces the previously published receipt
(`rg-only` context tokens 780.9M vs 781.0M; `work_quality` 92.5 vs 92.5), so
these numbers sit on the same footing as the existing headline:

|axis|skygrep-first|rg-only|
|---|---|---|
|path coverage|**100%**|100%|
|hit@1|**21.7%**|n/a — a scanner does not rank|
|hit@3|**33.3%**|n/a|
|MRR|**0.335**|n/a|
|context tokens|9.8M|780.9M (**80×**)|
|work quality|92.5|88.4|

Receipts: `benchmarks/reports/general-v2-2026-08-31/`.

The shape of it: **recall is excellent, ranking is not.** The expected file is
retrieved every time and is the top hit a fifth of the time. `cobra` alone
scores 50.0/70.0, which is why a small-repository reading of this is
misleading — all five real repositories land at 10–30% hit@1.

## Question 1 — is ranking a scoring problem or a candidate problem?

The agent presets pin `--no-rerank` for bounded latency, so every number above
was measured with the cross-encoder off. `skygrep-rerank` is the same policy
body with reranking enabled on the initial retrieval — the one call whose
ordering the rank axes measure.

The reranker's default model, `mixedbread-ai/mxbai-rerank-large-v2`, is on
Hugging Face. That is why this cannot run here.

## Question 2 — how do we rank against something that also ranks?

Every comparison so far is against `ripgrep`, which emits matches in traversal
order and has no ranking to compare with. `ck-sem` and `ck-hybrid` are
registered arms; `ck`'s ONNX model is on Hugging Face, so they cannot run here
either. Until one of them runs, "our ranking is good/bad relative to the field"
is not a claim this project can make.

## Run it

```bash
git clone -b agent/relicense-apache-2.0-citation \
    https://github.com/danielchen26/skylakegrep.git
cd skylakegrep
python3 -m venv .venv && ./.venv/bin/pip install -e '.[rerank,benchmark]'

# Ten-minute smoke check on the smallest repository first.
bash benchmarks/handoff_unblocked.sh /tmp/oss-root cobra

# Then the full six (hours; indexing dominates, react is the long pole).
bash benchmarks/handoff_unblocked.sh /tmp/oss-root
```

The script refuses to start if anything is still blocked, on purpose: an arm
that cannot fetch its model produces a **miss**, not an honest zero, and a
benchmark that records that as a competitor's score is worse than no benchmark.
It prints the four-arm table and how to read it at the end.

## How to read the result

|observation|conclusion|
|---|---|
|`skygrep-rerank` ≫ `skygrep-first`|scoring resolution; the reranker is the fix, and the work is making it cheap enough to leave on|
|`skygrep-rerank` ≈ `skygrep-first`|candidate generation; the fix is a lexical/BM25 lane, not a better scorer — note `path_precision` is 8.9% and every task's ground truth is a literal identifier|
|`ck-sem` ≫ both|a real ranking gap to close, and the first honest ranking comparison this project has had|
|`ck-sem` ≪ both|recall and ranking both favour us, and the benchmark is credible precisely because a funded competitor was in it|

Any of the four is a publishable result. Two of them are unflattering, which is
the point: `benchmarks/reports/general-v2-2026-08-15.json` already states that
this harness is "designed to expose misses rather than force a win".

## Caveat on the ck arms

`_ck_step` is unit-tested against mocked subprocess output — its JSONL parsing,
argument construction, and three failure paths are covered — but it has **never
completed an end-to-end run**, because `ck --index` cannot initialise on the
machine it was written on. Treat the first real invocation as unverified and
read `ck:sem`'s payload before trusting its scores.

It raises `CkUnavailable` rather than returning an empty result when ck is
missing, times out, or exits non-zero. That is deliberate. The same conflation
in the opposite direction already produced one wrong receipt in this
repository: with an embedding model absent, `skygrep search` printed `[]`, exited
`0`, and the harness scored it as "nothing found".

## Also waiting on a human

Three pull requests are open and mergeable. **#8 first** — it fixes
`index <path> --reset` deleting the database of the *current working
directory's* project instead of `<path>`'s, which loses user data. Verified
independently: 17 passed on its own tests, 422 on the full suite, no conflict
with #11.
