# skylakegrep 0.5.8.7 — adaptive query-plan routing

0.5.8.7 turns the routing fix from a patch into a principle: filesystem
metadata is no longer treated as a mutually exclusive search intent.
It is now a query-plan facet.

That distinction matters for both human CLI use and agent wrappers. A
query such as "show recently created files" can be answered entirely from
filesystem metadata and should return immediately. A query such as "show
me where my project brief that I recently created in PROJECT folder"
contains a recency constraint, but the answer still depends on locating a
specific target. In that second case, metadata can influence ranking, but
it is not allowed to skip filename, lexical, or semantic retrieval.

## What changed

- Added a structured metadata facet to the router:

  ```
  metadata_kind = opened | modified | created | size
  metadata_terminal = true | false
  ```

  Terminal metadata queries still use the fast filesystem lane. Composite
  queries keep searching and use metadata only as a cheap reranking signal
  inside already-relevant results.

- Added a `created` metadata dimension alongside `opened`, `modified`,
  and `size`.
- Added an identifier-collision guard so code tokens such as `created_at`
  are not misread as filesystem creation-time requests.
- Added CJK terminal/modifier separation for queries such as:

  ```
  最近打开过的文件        -> metadata terminal
  我最近打开过的合同在哪  -> metadata modifier, continue search
  ```

- Router decisions now carry metadata-facet provenance internally, and
  human output can show `metadata=<kind>:terminal` or
  `metadata=<kind>:modifier` in the router lane.
- JSON / agent paths keep the stable required schema while filename
  anchors for semantic-depth queries can include optional evidence fields:
  `query_excerpts`, `content_excerpt`, `content_preview`,
  `extracted_text_source`, and `extraction_note`.
- The release privacy gate now scans `benchmarks/` as well as package,
  docs, tests, and GitHub workflow surfaces. Historical local absolute
  paths in benchmark probes were replaced with env-driven placeholders.

## Verified routing receipts

All examples below use fictional placeholder names.

```
show recently created files
  intent=mixed source=fast-metadata oos=recency metadata=created:True
```

This stays a zero-semantic fast path.

```
show me where my project brief that I recently created in PROJECT folder
  intent=filename source=fallback-rules oos=None metadata=created:False
```

The recency phrase is treated as a modifier, not the whole answer.

```
最近打开过的文件
  intent=mixed source=fast-metadata oos=recency metadata=opened:True
```

This remains a fast metadata answer.

```
我最近打开过的合同在哪
  intent=mixed source=fallback-rules oos=None metadata=opened:False
```

The target term keeps deeper search enabled.

```
how does created_at field work
  intent=semantic source=fallback-rules oos=None metadata=None:False
```

Identifier-like code tokens are not hijacked by metadata routing.

## Verification

- Full test suite: `270 passed, 2 warnings`.
- Targeted routing suite: metadata terminal/modifier separation,
  CJK terminal/modifier separation, code-identifier collision guard,
  LLM `out_of_scope` override for composite queries, and metadata
  reranking of already-relevant candidates.
- Source privacy scan: `privacy release scan clean`.
- Package privacy scan after wheel/sdist build: required before upload.
- `git diff --check`: clean.

## Compatibility

- No index schema change.
- No required index rebuild.
- Public CLI flags are unchanged.
- Required JSON fields are unchanged. New evidence fields are optional
  and appear only when a semantic-depth filename anchor has extractable
  document/text content.
- Router cache version is bumped so stale cached decisions from earlier
  releases cannot preserve the old metadata-as-intent behavior.

## Follow-ups

- Promote the structured query plan into the public `--json`/agent
  contract once the agent schema stabilizes.
- Add explicit user-facing depth controls for `path`, `summary`,
  `evidence`, and `answer` modes while preserving the adaptive default.
