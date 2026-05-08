# skylakegrep 0.5.8.5 — multilingual routing hardening without losing semantic recall

0.5.8.5 fixes the real terminal and routing regressions uncovered after
0.5.8.3: smart quotes were being passed as literal characters, bare
multi-word queries were rejected by Click, multilingual filename
questions were not always routed through the fast filename layer, and
proactive outside-path search could run for queries that were not
actually filename lookups.

The release keeps the core guarantee intact: lexical and filename
signals can make the first answer faster, but normal code/content
queries still keep semantic cascade coverage instead of being reduced to
a conservative `rg` answer.

## What changed

- The CLI now accepts natural-language queries as bare words, quoted
  strings, or smart-quoted pasted text:

  ```
  skygrep where is my case42 file in Downloads
  skygrep -x where is my case42 file in Downloads
  skygrep “where is my case42 file in Downloads”
  ```

- Added a generic cheap filename pre-router for English, Chinese, and
  mixed-language file lookups. Examples:

  ```
  我的 CASE42 文件在哪  -> CASE42
  我的合同文件在哪   -> 合同
  where is package.json -> package.json
  ```

- Added a cheap semantic pre-router for obvious `how`, `why`,
  `explain`, `describe`, `trace`, and `summarize` questions. These skip
  the LLM router startup path but still run the semantic cascade.
- Filename answers can return immediately when filename evidence is
  already decisive. If the index is not ready, background indexing can
  still continue, but the user gets the known filename answer first.
- Literal/rg evidence no longer short-circuits the semantic cascade for
  general code/content questions. It is used as evidence, then the
  semantic layers still run.
- Proactive outside-path diffusion is now scoped to filename intent, so
  wrong-folder recovery still works for file questions without slowing
  or polluting ordinary semantic searches.
- Router cache versioning was bumped so stale cached decisions cannot
  override the new routing behavior.

## Verified

- Full test suite:
  `pytest -q tests` -> `233 passed, 2 warnings, 19 subtests passed`.
- Static diff check:
  `git diff --check` -> clean.
- Python 3.9 package smoke:
  install the built wheel into a clean venv, import
  `skylakegrep.__version__`, confirm `skygrep --help` exposes the
  `search` command, then run
  `skygrep -x where is my case42 file in Downloads` successfully.
- Real terminal replay of the reported smart-quote case:
  `skygrep “where is my case42 file in Downloads”` returns the CASE42
  filename matches and exits 0 instead of failing Click parsing.
- Real bare-query replay:
  `skygrep -x where is my case42 file in Downloads` routes as one query,
  prints `path: filename-lookup`, and returns the CASE42 files.
- Multilingual filename replay:
  `skygrep -x "我的 CASE42 文件在哪"` routes through
  `heuristic-filename` with primary token `CASE42` and returns the CASE42
  filename results.
- Semantic edge replay:
  `skygrep -x "how does cascade decide"` routes through the semantic
  path and does not trigger proactive filename diffusion.
- Wrong-folder proactive replay:
  a filename query from a directory that does not contain the target
  expands outward and finds the matching file outside the current path.

## Compatibility

- No index schema change.
- No required index rebuild.
- Public CLI flags are unchanged.
- JSON output shape is unchanged.
- Previously invalid bare or smart-quoted query invocations now work.
