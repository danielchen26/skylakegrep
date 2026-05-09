# skylakegrep 0.5.9 — generic adaptive routing and scoped search performance

0.5.9 is a product-level routing release. It keeps the existing CLI and
JSON surfaces compatible, but changes how skylakegrep plans a query before
choosing retrieval lanes.

The core shift: a user query is no longer treated as one terminal intent.
It can contain independent facets at the same time:

- **scope** — the folder / repo / workspace the user wants searched;
- **target** — the file, artifact, symbol, or document clue;
- **metadata** — created / opened / modified / size as either a full
  answer or a ranking constraint;
- **answer depth** — path-only, preview, evidence, or deeper semantic
  content.

The router can still use a small local model, but model output is not
trusted blindly. Filesystem scope, filename evidence, lexical evidence,
metadata facets, and semantic depth each have their own validation gate.

## What changed

- Added a first-class **query scope facet**. Phrases such as
  `in CASE42 folder`, `inside Research Workspace`, and
  `在合同档案文件夹...` now resolve to a concrete local root before
  retrieval starts. Scope constrains every lane and prevents broad hidden
  / tool-directory sweeps from outranking the folder the user actually
  named.
- Scope clauses are stripped from the text that is sent to fast-intent,
  the LLM router, metadata analysis, and lexical gates. This prevents the
  folder name from becoming the primary filename token or semantic target.
- Metadata is now a plan facet, not a terminal intent by default.
  `show recently created files` remains a zero-semantic filesystem
  answer, while `show where project brief recently created in PROJECT
  folder` keeps filename / lexical / semantic retrieval alive and uses
  creation time only as a modifier.
- CJK and mixed-language scope handling is generic at the grammar layer:
  scope suffixes such as `文件夹`, `目录`, and `项目` are recognized even
  when the query continues immediately after the suffix.
- Warm scoped semantic queries can finish from strong lexical evidence
  without waiting for expensive cascade / rerank. The fallback is scoped
  and evidence-based: a small result set plus snippet hits can satisfy the
  default human view even when the path vocabulary differs from the query.
- JSON / agent calls benefit from the same scoped lexical shortcut. A
  structured `--json --detail summary` call can now return the relevant
  code and document snippets directly instead of waiting for cascade when
  lexical evidence is already sufficient.
- Markdown, text, PDF, and docx content now participate more consistently
  in indexing. Text-like files are chunked directly; PDF/docx files use the
  existing bounded text extraction path.
- Filename lookup ranking now considers basename coverage, query-term
  coverage, hidden-path penalties, document/code extension priority, and
  metadata facets. This keeps concrete document anchors above unrelated
  generated or hidden files.
- Human output no longer says `sibling-folder semantic search` unless a
  sibling-folder pass actually ran. When lexical evidence is final, the UI
  says `keyword matches` and marks `cascade-skipped`.

## Before vs after

The release benchmark uses only synthetic placeholder files and folders.

| Query shape | Previous behavior observed during validation | 0.5.9 behavior |
| --- | --- | --- |
| Scoped filename lookup | Could wait behind broad scans or semantic setup | `0.397s`, `path=filename-lookup` |
| Scoped semantic content | Could wait ~7 s because scope polluted routing | `0.338s`, `source=fast-intent` |
| Metadata-only scoped query | Could fall into content search | `0.279s`, `path=metadata-created` |
| Metadata modifier query | Could wait for LLM or be treated as metadata-only | `0.379s`, `metadata=created:modifier` |
| Folder names with spaces | Could approach multi-second LLM routing | `0.401s`, scoped fast plan |
| CJK filename scope | Could miss the folder scope | `0.370s`, scoped filename lookup |
| CJK + English semantic scope | Could miss scope and search outside | `0.333s`, scoped lexical evidence |
| Wrong-directory filename lookup | Keeps bounded proactive recovery | `0.372s`, proactive filename result |
| Warm vocabulary-mismatch semantic query | Could wait 9-16 s for cascade/rerank | `0.453s`, scoped lexical evidence |
| Warm JSON agent query | Could wait ~9.5 s | `0.877s`, JSON `rg-shortcut` results |

## Release benchmark

The 0.5.9 release gate ran a 12-case real CLI benchmark on a synthetic
workspace:

```text
OK filename_scope_simple              0.397s
OK semantic_scope_content             0.338s
OK metadata_terminal_scope            0.279s
OK metadata_modifier_scope            0.379s
OK scope_with_spaces_and_modifier     0.401s
OK cjk_filename_scope                 0.370s
OK cjk_semantic_scope                 0.333s
OK wrong_dir_proactive_no_scope       0.372s
OK json_filename_scope                0.334s
OK warm_semantic_vocab_mismatch       0.453s
OK warm_code_identifier_collision     0.616s
OK warm_json_semantic_agent_shape     0.877s
```

These cases cover cold-start, warm-index, filename, semantic, metadata
terminal, metadata modifier, folder names with spaces, CJK / mixed
language, wrong-directory proactive search, code identifier collisions,
and JSON / LLM-agent output shape.

## Verification

- Full test suite: `295 passed, 20 subtests passed`.
- Targeted routing suite: `122 passed, 3 subtests passed`.
- End-to-end synthetic CLI benchmark: `12 / 12 passed`.
- `git diff --check`: clean.
- Source privacy scan: required before build.
- Wheel/sdist privacy scan: required before upload.

## Compatibility

- No CLI flag was removed.
- Required JSON fields are unchanged.
- No user action is required for existing project indexes. Text/PDF/docx
  coverage improves as indexes refresh or rebuild naturally.
- Router cache entries are keyed by the scope-stripped routing query, so
  stale folder-token routing decisions are not reused for scoped variants.

## Privacy note

All examples in this release use fictional placeholders such as `CASE42`,
`PROJECT folder`, `project-report.pdf`, and `合同档案`. No real user prompt,
private filename, private folder name, local machine path, screenshot, or
document category is included in the release notes, tests, docs, wheel, or
sdist.
