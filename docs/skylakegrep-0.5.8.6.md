# skylakegrep 0.5.8.6 — fast path answers without sacrificing semantic depth

0.5.8.6 tightens the routing contract for both direct CLI use and agent
wrappers such as Claude Code, Codex, and OpenCode. Filename evidence is
now treated as an answer only for path-depth questions. When the query
asks about content, behavior, or explanation, the filename match stays
as an anchor and the same invocation continues through lazy semantic
refinement.

The goal is simple: fast when the answer is a path, deeper when the
question needs understanding.

## What changed

- Human output now shows the active routing lane by default:

  ```
  🧭 router: semantic · primary_token="CASE42_Project_Report" · conf=0.81 · source=fast-intent
  ```

  `--explain` still adds the detailed reason, per-result `via:` lines,
  and cascade-lane evidence.

- Filename finality is now answer-depth gated. A concrete filename hit
  can finish a path-depth query, but it cannot terminate semantic or
  synthesized-answer queries.
- Semantic queries that include a filename-like clue keep the filename
  tier enabled. The file is used as an anchor while semantic/cascade
  retrieval remains active.
- Cold-start semantic queries now stream a better first stage: if the
  filename tier finds an anchor, standard output shows a bounded content
  preview from that file before lazy refinement continues.
- Lazy refinement can upgrade a preliminary filename card to a deeper
  semantic/content result for the same path instead of losing the deeper
  result to path-only de-duplication.
- Cross-folder lazy expansion is no longer triggered when the current
  scope already found a filename anchor. This avoids slow, noisy home-root
  diffusion for semantic questions that already have a local file to
  inspect.
- Metadata questions such as "latest files I opened" use a fast
  filesystem metadata lane before Ollama/router/index work.
- Installation docs now use `python3 -m pip install --user`, list Ollama
  model pulls one per command, and include a clean uninstall/reinstall
  path for users with multiple Python installs.

## Verified examples

All examples below use fictional placeholder names and temporary test
directories.

### Path-depth

```
skygrep "where is CASE42 file"
```

Verified output shape:

```
🧭 router: filename · primary_token="CASE42" · conf=0.95 · source=fast-intent
✓ 0.031s · quality=BEST
   path   : proactive-filename
   pool   : 0 filename + 0 rg + 1 proactive · lazy-skipped
   index  : building in background
```

Wall time in the release verification run: `0.40s`.

### Semantic-depth with a filename anchor

```
skygrep "what does CASE42_Project_Report say about retries"
```

Verified behavior:

```
🧭 router: semantic · primary_token="CASE42_Project_Report" · conf=0.81 · source=fast-intent
▾ preliminary filename anchors + keyword matches (lazy semantic refinement starting…):
│ CASE42 retry policy uses exponential backoff after transient failures.
🔍 lazy auto-trigger · scanning project structure…
⚡ embedding 1 files (1 Ollama call)…
▾ lazy semantic search added no new matches (top-K above is the final answer).
```

Wall time in the release verification run: `2.44s`.

### Metadata lane

```
skygrep "what is the latest 2 files i opened"
```

Verified output shape:

```
✓ 0.001s · quality=BEST
   path   : metadata-opened
   pool   : 2 files · semantic-skipped
```

Wall time in the release verification run: `0.22s`.

## Verification

- Full test suite:
  `253 passed, 2 warnings`.
- Source privacy scan:
  `privacy release scan clean`.
- Routing-depth checks:
  - `where is CASE42 file` -> `intent=filename`, `filename_final=True`.
  - `what does CASE42 file say about retries` -> `intent=semantic`,
    `skip_cascade=False`, `filename_final=False`.
  - `explain CASE42_Project_Report logic` -> `intent=semantic`,
    `primary_token=CASE42_Project_Report`, `skip_filename=False`,
    `filename_final=False`.
  - `how does cloud code call skygrep for routing` -> `intent=semantic`,
    `skip_cascade=False`, `filename_final=False`.

## Compatibility

- No index schema change.
- No required index rebuild.
- Public CLI flags are unchanged.
- JSON output shape is unchanged.
- Human output now includes the concise router lane by default; use
  `--json` for machine-readable output without human status lines.
