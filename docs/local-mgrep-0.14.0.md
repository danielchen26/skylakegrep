# local-mgrep 0.14.0 — release notes

The routing model changes from **mutually-exclusive tiers** (only
one of filename / lexical / cascade runs) to **hierarchical merge**
(every enabled tier contributes; results are deduplicated by path
and ranked by `classify_intent(query)`). The user gets filename
matches AND content matches in one shot — the right tier wins
top slots based on query phrasing.

## Headline

```
$ mgrep "where is config file" -m 5

╭─ local_mgrep/src/config.py                            py   1.000
│ size: 5.3 KB    modified: 2026-05-04    type: py
╰────────────────────────────────────────────────────────────────

╭─ local_mgrep/src/integrations.py:87-109           python   0.609
│ symbol: register
│
│ def register(self) -> bool:
│     """Append the snippet to the integration's config file."""
│     ...
╰────────────────────────────────────────────────────────────────

╭─ local_mgrep/src/storage.py:292-328               python   0.608
│ symbol: file_level_search
│ ...
╰────────────────────────────────────────────────────────────────

[1.218s · intent=filename · 1 filename + 0 lexical + cascade · ...]
```

The filename match (`config.py` itself) is pinned to top because
``classify_intent("where is config file") == "filename"``. Below
it: cascade chunks discussing config — semantic context that
v0.13.0 would have hidden.

For descriptive queries the order flips:

```
$ mgrep "how does the cascade decide which path to take" -m 3

╭─ local_mgrep/src/storage.py:822-...                python   N.NN  ← cascade dominant
│ ...
╰─────

[... · intent=semantic · 0 filename + 0 lexical + cascade ...]
```

## What's new

### `local_mgrep/src/intent.py` (new)

Owns intent classification + multi-tier merging.

```python
from local_mgrep.src.intent import classify_intent, merge_results

intent = classify_intent("where is eb1b file?")  # → "filename"
intent = classify_intent("how does X work")      # → "semantic"
intent = classify_intent("auth login token")     # → "lexical"
intent = classify_intent("")                     # → "mixed"

merged = merge_results(
    filename=fn_results,    # from filename_shortcut
    lexical=rg_results,     # from lexical_shortcut
    semantic=cascade_results,
    intent=intent,
    top_k=10,
)
```

`merge_results` deduplicates by path (keeping the higher-priority
tier's representation under the detected intent) and ranks by
`(tier_priority, -score)`.

Tier priority by intent:

| Intent | filename-lookup | rg-shortcut | cascade |
| --- | :---: | :---: | :---: |
| filename | **1st** | 2nd | 3rd |
| semantic | 3rd | 2nd | **1st** |
| lexical  | 3rd | **1st** | 2nd |
| mixed    | tied | tied | tied |

### Routing model: every enabled tier always runs

Previously (v0.12.0 → v0.13.0):

```
filename hits?  → return filename only, skip everything
lexical hits?   → return lexical only, skip cascade
otherwise       → cascade
```

Now (v0.14.0):

```
fn_results = filename_shortcut(...)   # 10 ms, never blocks downstream
rg_results = lexical_shortcut(...)    # 50 ms, also collected
sem_results = cascade_search(...)     # full cascade always runs
return merge_results(fn, rg, sem, intent=classify_intent(query))
```

A query like ``where is config file`` returns the filename match
**and** the relevant code chunks; ``how does config get loaded``
returns the cascade chunks **and** any incidental filename matches
as low-priority context.

### Telemetry line shows the merge

The trailing status line now exposes the routing decision:

```
[1.218s · intent=filename · 1 filename + 0 lexical + cascade · ...]
```

You can see at a glance which tier was deemed primary and how
many results each tier contributed before deduplication.

### Cold-start (no-index) path also merges

When the project's index isn't ready yet, the cold-start ripgrep
fallback is now merged with `filename_shortcut` results too — so
even a fresh repo answers `mgrep "where is README"` correctly
without needing to wait for the embedder.

## Files changed

  - `local_mgrep/src/intent.py` (new) — `classify_intent()` and
    `merge_results()`. ~140 lines.
  - `local_mgrep/src/cli.py` — filename + lexical shortcut blocks
    converted from "early-return on hit" to "collect into local
    list". After cascade, the three result lists are merged via
    `intent.merge_results`. Cold-start rg path also merges with
    filename hits.
  - `tests/test_intent.py` (new) — 19 tests covering intent
    detection across query phrasings + merge dedupe + tier
    priority + top-k truncation + empty inputs.
  - `pyproject.toml`: 0.13.0 → 0.14.0.
  - `docs/local-mgrep-0.14.0.md` (this file).
  - `docs/index.html`, `docs/assets/{og-image.svg,og-image.png,
    hero-dark.svg}` — version stamp.
  - `docs/README.md`, `README.md` — index entry / release bullet.

## Compatibility

  - **88 / 88 unit tests pass** (47 original + 8 lexical shortcut
    + 9 filename shortcut + 24 intent classifier / merger).
  - All 0.4.x – 0.13.0 flags / env / per-project DB layout
    unchanged.
  - Existing project indexes are picked up as-is.
  - `--json` output schema is unchanged — but the merged result
    list now contains a richer mix (filename + lexical + cascade
    items) instead of just one tier's. Downstream JSON consumers
    that filtered by `fallback` field continue to work; consumers
    that assumed all items came from cascade should expect mixed
    results now.
  - `--no-filename-shortcut` and `--no-rg-shortcut` flags retained;
    they now mean "skip this tier in the merge" rather than
    "disable this tier's early-return".
  - Pipe / redirect (`| cat`, `> file.txt`) still falls back to
    plain text without ANSI colour.

## Latency note

Every query now pays the full cascade cost (sub-second in the
warm cheap path, up to ~3 s when the cascade escalates). The
v0.13.0 fast path for clear filename queries (~10 ms) is gone
**by design** — the user explicitly asked for the merge model so
filename and content results both surface.

If you specifically want the fast filename-only path back:

```
mgrep "where is X file" --no-rg-shortcut --no-cascade
```

(``--no-cascade`` skips the semantic pipeline entirely.)

## What did not change

  - Cascade retrieval pipeline (file-mean cosine + HyDE
    escalation + rerank + PageRank tiebreaker).
  - Lexical content shortcut and its four-condition gate.
  - Filename-lookup shortcut and its four-condition gate.
  - All flags / env vars from prior releases.
  - JSON output schema fields.
  - Default models.
  - Pygments-based card rendering (v0.13.0).

## Honest accuracy gates

  - **Unit tests** — 88 / 88 pass.
  - **Intent classifier tests** — 18 / 18 pass across filename /
    semantic / lexical / mixed / extension-hint / empty branches.
  - **Merge tests** — 6 / 6 pass covering tier priority +
    dedup-by-path + top-k truncation + empty inputs.
  - **CLI smoke (filename intent)**: `mgrep "where is config file"`
    in the local-mgrep repo returns `config.py` first, then 4
    cascade chunks discussing config loading. Both tiers
    contribute, intent="filename" promotes the filename match
    to top.
  - **CLI smoke (semantic intent)**: `mgrep "how does the auth
    token get refreshed"` in a code repo runs cascade
    unaffected, ranks cascade chunks first, no irrelevant
    filename clutter.
  - **CLI smoke (downloads, no-index dir)**: `mgrep "where is
    eb1b file?"` in `~/Downloads/` returns 5 EB1B documents
    via filename tier; cascade returns nothing useful (PDFs not
    indexed) and is correctly ranked below the filename hits
    by intent priority.

## Install

```
pip install --upgrade local-mgrep
```
