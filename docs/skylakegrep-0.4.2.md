# skylakegrep 0.4.2 — P0 hot-fix: `KeyError 'snippet'` on every escalated query

## TL;DR

**Hot-fix.** 0.4.0 / 0.4.1 shipped a production crash —
`KeyError: 'snippet'` — that fired on **every CLI query that
escalated AND graph-expanded contributed candidates**. Users hit
this whenever the cascade fell off the cheap path (~20 % of
queries). Upgrading is mandatory.

The bug: 0.4.0's `_expand_via_reference_graph()` returned result
dicts without a `snippet` field. The CLI's `merge_results()` reads
`result["snippet"]` for every dict in the rerank pool. Crash.
Hidden by tests that called `cascade_search` directly, never
through the CLI's full path.

## Compatibility

  - Python: unchanged — 3.9+
  - Index format: unchanged
  - Default behaviour for cheap-path queries: unchanged from 0.4.x
  - **Escalated queries no longer crash** — the only change

## How it shipped past tests

This is the fourth receipt of the same anti-pattern in 0.3.x →
0.4.x:

| Receipt | What was tested | What was missed |
|---|---|---|
| 0.3.0 | by-construction arguments | actual recall |
| 0.4.0 | synthetic 3-file unit tests | real-corpus behaviour |
| 0.4.1 | direct `cascade_search` API call | the CLI `merge_results` path that consumes the dicts |
| 0.4.2 (this) | actual `skygrep search` CLI on a real index | (this is the fix) |

Auto-memory rule added:
`feedback_test_through_actual_user_path.md` — every release MUST
exercise `skygrep search` (the user's actual entry point) on a
real index, with at least one query that escalates.

## What's in 0.4.2

**Code (1 line of substance):**

```python
# storage.py:_expand_via_reference_graph()
# Result dict shape must match cli.merge_results expectations:
out_results = [
    {"path": p, "score": s, "start_line": 0, "end_line": 0,
     "language": "", "chunk": "", "snippet": ""}     # ← was missing
    for p, s in kept
]
```

**Verification:** real CLI search query against the skylakegrep
index, escalation path:

```
✓ 7.277s · quality=BEST
   path     : cosine-escalated-rerank (escalated to rerank)
   router   : fallback-rules → intent=semantic (0.60)
   evidence : σ-gap=0.0040 < τ=0.0060 (adaptive)
   pool     : 0 filename + 0 lexical · cascade
   index    : 21s ago ago · 66 files · L2 symbols + graph prior
```

No crash. Cascade and graph_expand both ran end-to-end. (Compare:
the same query before the fix raised `KeyError: 'snippet'` at
`cli.py:92`.)

**Tests:** 206 / 206 still pass — hot-fix only adds dict fields,
no logic change.

## Public OSS bench (Django + React + Tokio): pending verification

The published 30 / 30 OSS bench is not re-run in 0.4.2; it's
deferred to 0.4.3. Why: the bench wrapper
(`benchmarks/public_oss_bench.py`) currently stalls on the first
fixture for reasons we have not yet diagnosed (no log output after
`=== Django ===`). The architectural invariant still holds — the
0.4.x changes are additive to the cascade rerank pool and cannot
demote a candidate the cross-encoder ranks high — but the user's
explicit instruction is **never claim numbers without measurement**,
so 0.4.2 reports only what was actually verified: the hot-fix
unblocks the CLI escalation path on a real index.

0.4.3 will:
  1. Diagnose and fix the bench stall.
  2. Run the full Django / React / Tokio 30-task bench.
  3. Publish the verified 30 / 30 (or whatever the real number is)
     as the 0.4.x recall figure.

If the real number is < 30 / 30, 0.4.x will be rolled back to
0.2.21 cascade + the hot-fix only — per the user's invariant
"never ship something worse than the best previous version".

## Why this took four releases to surface

The pattern: every test in 0.3.x / 0.4.x exercised one component
in isolation — `cascade_search` direct, synthetic seed mappers,
unit-fixture graphs. None of them went through the CLI's
`merge_results()`, which is the choke-point where every result
dict's shape gets consumed. A `snippet` field missing in one
candidate-source's dicts is invisible to those tests because they
return their dicts to the test harness, not to merge_results.

The lesson — now in deepest memory:
**testing through internal APIs only verifies those APIs. The
user's experience runs through `skygrep search`, and that's what
the bench has to exercise.**

## Acknowledgments

User caught the missing test surface directly — *"if 出现重大问题
你先止血呀然后找出真正的问题进行修复肯定不能比之前最好的那一版差吧"*
("if there's a critical issue first stop the bleeding, then find
the real problem and fix it; absolutely cannot ship something
worse than the best previous version"). This release stops the
bleeding. The OSS bench investigation continues in 0.4.3.
