# Plan — Conversational session state for skygrep queries

**Date filed:** 2026-05-05
**Status:** Open · design only, no code
**Trigger:** User feedback during 0.2.12 review:
> 他并没有基于上面给我我认为不对的答案继续给我答案这个问题你怎么解决
> 现在我们并没有让当前的状态 condition on 我们过去问的问题对吧

---

## Problem statement

Every `skygrep` invocation today is **stateless**. The router
re-classifies intent from scratch, the cascade re-retrieves
without knowledge of previous results, and the renderer doesn't
know whether the user just asked a similar question 30 seconds
ago. Real shells / chat tools (the user is comparing to
Superconductor / Warp / Cursor) maintain a session: the next
prompt is interpreted in the context of the previous one.

Two distinct conversational patterns emerge from the user's
example:

  1. **Follow-up refinement** — "give me more like that", "show
     the next page", "narrow to PDFs" — the user wants a related
     query that BUILDS ON the previous one.
  2. **Negative feedback** — "the answer was wrong", "that's not
     what I meant", "try again" — the user is FLAGGING the
     previous answer as bad and wants the system to retry with
     that signal.

Both require **session state**. They're related but require
different downstream behaviour:

  - Pattern 1 → reuse the previous query's primary_token /
    cascade scope; broaden or narrow the search.
  - Pattern 2 → de-rank or exclude the previous result paths;
    treat the previous answer as a negative signal.

---

## Design space

### Where does session state live?

Three options, each with trade-offs:

  - **Per-shell-session via env var.** Set `SKYGREP_SESSION_ID`
    in the shell prompt; skygrep reads/writes session state in
    `~/.skylakegrep/sessions/<sid>.json`. Pros: tied to terminal,
    survives across invocations within one shell, dies when shell
    exits (no cross-tab pollution). Cons: requires shell
    integration script (zsh / bash / fish).
  - **Globally last-N queries** in metadata table. Pros: zero
    setup. Cons: cross-tab pollution; "previous query" may have
    been from a different terminal / project.
  - **Per-project** (i.e. per index DB). Pros: scoped to the
    repo the user is working in. Cons: not sensitive to
    cross-project shell sessions; same project across two tabs
    leaks.

**Recommended:** start with per-project (cheapest, no shell
integration required), iterate to per-shell when the user
explicitly asks for it. Skygrep already has a metadata table per
DB; one new table is enough.

### What does session state look like?

```sql
CREATE TABLE query_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    query           TEXT,
    intent          TEXT,
    primary_token   TEXT,
    top_paths       TEXT,    -- JSON array of result paths
    timestamp       REAL,
    feedback        TEXT     -- 'positive' / 'negative' / null
);
```

Last-N rows = the conversational context.

### Detecting follow-ups

This is the hard part. Two complementary approaches:

**Approach A: LLM router classifies follow-up.** Extend
`route_query()` to detect when a query is a follow-up to the
previous one. The LLM sees the previous query in its prompt:

```
Previous query (30s ago): "where is my eb1b file?"
Current query:            "give me the answer is wrong"
```

The LLM can return:

```json
{"intent": "feedback", "follow_up_kind": "negative",
 "reuses_token": "eb1b"}
```

Pros: principled, content-agnostic (the LLM understands what's a
follow-up regardless of phrasing). Cons: extra LLM cost on every
query (negligible since we already call the LLM router).

**Approach B: Conservative heuristic on session-history age.**
If the current query is < N seconds old AND short AND lacks
identifier-shape tokens itself, treat it as a likely follow-up.
Pros: zero extra LLM cost. Cons: more brittle than LLM detection.

**Recommended:** A as primary, B as offline fallback (mirrors the
0.2.6 out-of-scope architecture).

---

## Three concrete patterns to support

Once detection works, what does the system actually DO?

### P1 — Negative feedback ("answer wrong")

```
$ skygrep "where is my eb1b file?"
[result A]   ← wrong answer

$ skygrep "the answer is wrong"
↳ Detected as negative-feedback follow-up to previous query
  (30s ago: "where is my eb1b file?"). Re-running with previous
  answer ([result A]) on the EXCLUDE list.

[result B, with [result A]'s paths suppressed]
```

Implementation: add `excluded_paths` to `cascade_search`'s scope
filter; populate from previous query's top_paths.

### P2 — Follow-up refinement ("show me more")

```
$ skygrep "auth flow code"
[result A, B, C]   ← top 3

$ skygrep "show me more"
↳ Detected as continuation. Showing top 4-10 from the previous
  cascade run.

[result D, E, F, G, H, I, J]
```

Implementation: cache the previous cascade's full ranking (first
~50 candidates); on follow-up, paginate.

### P3 — Refinement with new constraint ("narrow to PDFs")

```
$ skygrep "auth flow code"
[mixed results]

$ skygrep "narrow to PDFs"
↳ Detected as refinement. Re-running previous query with
  --include='*.pdf' filter.

[results restricted to .pdf]
```

Implementation: LLM extracts the new constraint as a filter
expression; re-run with merged filters.

---

## Implementation phases

### S-1 — query_history table + per-project last-N (week 1)

  - Add `query_history` table to `init_db()`
  - On every search: write a row with query / intent / top_paths
  - On every search: read last-N (default 5) rows for the LLM
    router context
  - Add `skygrep history` subcommand for the user to inspect
  - **Doesn't change retrieval behaviour yet** — just builds the
    foundation.

### S-2 — LLM follow-up detection (week 2)

  - Extend `_ROUTER_PROMPT` with the previous query as context
  - Parse `follow_up_kind: none / negative_feedback / continuation
    / refinement`
  - Add `decision.follow_up: dict | None` field to RouterDecision
  - **Still doesn't change retrieval** — telemetry only, with
    routing footer surfacing the detection so users can see what
    the system thinks is a follow-up.

### S-3 — actual conditioning (week 3)

  - Implement P1 (negative-feedback exclusion) via
    `excluded_paths` in `cascade_search`.
  - Implement P2 (continuation paging) via cached ranking cache.
  - Implement P3 (refinement filter merge) via LLM-extracted
    constraint.

Each phase ships independently; users can opt-in to phases
gradually via env vars (`SKYGREP_SESSION_AWARE=1`).

---

## Open questions

  1. **Session scope** — per-project, per-shell, or per-user?
     The user's example was on Desktop (a non-project dir).
     Should sessions tie to *cwd* + recent-queries timestamp?
  2. **Privacy** — query history is sensitive (the user's actual
     questions). Where does the table live? Is it in the same
     SQLite file as embeddings (where the user already has a
     mental model of "this is the index, my queries get
     embedded into it") or separate (`~/.skylakegrep/history.db`)?
  3. **Decay** — old queries shouldn't pollute new ones. How long
     does "previous query" last? 5 min? Until the next
     non-follow-up query? Until end-of-day?
  4. **LLM cost** — extending the router prompt with previous
     query context grows the LLM input. We already pay for the
     router call; the marginal cost is small but non-zero.
  5. **CLI surface** — should follow-up detection be opt-in
     (`--continue` / `--feedback wrong`) or auto?  Auto is the
     "intelligent" answer per Principle 6, but requires
     unambiguous detection (mistakes are confusing).

---

## Risks

  - **Mistaken follow-up detection.** If the LLM thinks
    `"foo bar baz"` is a follow-up to a 5-min-old previous query,
    the user gets surprising behaviour. Threshold: confidence
    must be high; default to "treat as new query" on uncertainty.
  - **Session contamination across tabs.** Per-project state
    won't notice that the user opened a new terminal in the same
    project. Documented limitation — escalate to per-shell only
    if users actually report this.
  - **Privacy regression.** Query history written to the same DB
    as embeddings means a user copying their `.db` file (e.g. for
    portability) also copies their query history. Should be
    documented; consider a separate file with explicit opt-in.
  - **Combinatorial UX with proactive.** Session state ×
    proactive enhancers × cold-start = lots of interactions.
    Need to think about whether negative-feedback should also
    suppress proactive's previous-query suggestions.

---

## Measurement plan (prerequisite)

Before building any of this, log query timing patterns for a
real user (the project author, anonymously) for 1-2 weeks. How
often does the user ask follow-up-shaped queries within 1 / 5 /
30 minutes? If the answer is < 5 % of total queries, this work
isn't worth shipping.

The user has already shown us at least one strong example
(`'我有没有跟"eb1b"有关的文件？'` → `'给我的答案不对'`), so
the rate is non-zero. But "non-zero" isn't enough — we need
"meaningful" before adding session complexity.

---

## Decision

Filed in `docs/plans/` per user instruction. **Not on a release
schedule** — depends on the measurement run + a user mandate to
proceed.

Until then, the user can manually express follow-up intent by
re-typing more context: `skygrep "the eb1b answer was wrong, ignore Project.toml hits"`. Less intelligent, but explicit. The
proactive `recovery_progress_hint` from 0.2.11 already covers
the "wait for index to build" flavour of negative feedback, so
the most acute bad-UX case is partially mitigated.

---

## Cross-references

  - [`docs/plans/2026-05-05-graph-prior-folder-inference.md`](2026-05-05-graph-prior-folder-inference.md)
    — when session state is available, query_history × folder
    co-occurrence (signal S4 in that plan) becomes much easier.
  - [`docs/plans/2026-05-05-phase-c-audit.md`](2026-05-05-phase-c-audit.md)
    — Phase C scheduler decisions can use session history as
    additional input (e.g. "skip rerank if last 3 queries all
    early-exited at high σ-gap").
  - [`docs/PRINCIPLES.md`](../PRINCIPLES.md) Principle 6 — the
    follow-up enhancer would be a third proactive enhancer
    alongside `filename_extend` and `recovery_progress_hint`.
