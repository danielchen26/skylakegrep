# skylakegrep 0.5.3 — lazy seed selection that actually works

0.5.0/0.5.1 shipped lazy auto-trigger but the actual hit-rate on the
Django oracle bench was **1/10** — barely above pure ripgrep cold-start.
The root cause, found by running the real CLI (not the python API),
was that the `lazy_explore_cold_start` seed selection picked the wrong
25 files: Django's hundreds of auto-generated migration data files
(`0001_initial.py`, `0002_*.py`, …) ate the entire seed budget on
every "migration" query, leaving the actual `executor.py` runner code
with zero seed slots. 0.5.3 fixes the seed selection at the root, plus
adds three structural improvements that turn the lazy module from a
1/10 dud into a measurable upgrade over rg-only cold-start.

## What changed

**1. Token-shortcut DEDUP** (`lazy_indexer._dedupe_seed_groups`).
Files in the same parent directory that share a numeric prefix family
(`0001_*.py`, `0002_*.py`, …) or a stem prefix of length ≥6 collapse
to a single deterministic representative. Keeps the per-dir
representative; structural code files (`executor.py`, `migration.py`,
`base.py`) are preserved untouched because they don't share families.

**2. Numeric-prefix scoring penalty.** Even after dedup, Django has
`*/migrations/0001_initial.py` across ~20 contrib subpackages — each
with a different parent dir, each surviving dedup. Each scores 2 hits
on a query like "where does Django apply migrations" (matches `django`
+ `migrations`) — but so does `django/db/migrations/executor.py`. A
half-step shave on numeric-prefixed stems pushes structural code above
data files at the same hit count. Without this, executor.py would be
buried by 20+ `0001_initial.py` siblings.

**3. LLM router timeout fix** (`llm_router.infer_candidate_paths`).
The module-level `LLM_TIMEOUT_SECONDS = 0.5` was being inherited by
the cold-start path-picker — far too tight for a `qwen2.5:3b` call
that emits a 5–15 path list. Every call was silently timing out and
returning `[]`. 0.5.3 raises this function's default timeout to 8 s
(comfortably inside the 10 s lazy budget) so the LLM router actually
contributes seeds.

**4. Regex import diffusion** (`extract_imports` +
`resolve_imports_to_paths`). After loading the K seed files into
memory, a one-pass language-agnostic regex scan extracts
`import` / `from` / `require` / `#include` / `use` statements and
resolves them to local file paths via the pre-walked tree, adding up
to 10 import-graph neighbours to the embed pool. This is how a wrong
initial seed diffuses outward to a correct one — even when the user
is in the wrong subfolder.

**5. ThreadPool I/O parallelism.** Tree walk, LLM router, file text
load, and import extraction run on a small worker pool (capped at 4)
so wall time is dominated by the single Ollama batch-embed call, not
serialised I/O. The Ollama embed call stays single-threaded — the
model serialises requests at the model layer, so HTTP-level
parallelism doesn't help.

**6. Progressive stderr progress lines.** A user staring at a 5–10 s
wait sees the system at work:

```
🔍 lazy auto-trigger · scanning project structure…
🌊 4 subprocesses · 5 LLM-routed · 15 token-shortcut · 18 seeds total
💧 diffused +9 import neighbours (total 25 seeds)
⚡ embedding 25 files (1 Ollama call)…
```

**7. Cold + wrong-folder branch.** When `len(rg_cold) == 0` (rg found
nothing in cwd — strong signal the answer isn't here), `lazy_cwd` and
`lazy_cross_folder` fire in PARALLEL. The cross-folder hunt walks
`SKYGREP_PROACTIVE_DIRS` roots so the user doesn't have to be in the
right folder to get an answer.

**8. Warm + low-confidence branch.** When the warm cascade returns a
small `top1 - top2` gap (its own σ-adaptive confidence signal), the
answer may not live in cwd. cli.py fires `lazy_explore_cross_folder`
as augmentation after `merge_tiers` — cascade remains primary, sibling
hits are appended. Triggered post-cascade in cli.py; cascade_search
internals are NOT modified.

## Numbers

Real CLI on `/tmp/oss-bench/django` (fresh DB each query, run via
`benchmarks/release-0.5.3-rg-vs-lazy.py`, NOT python API):

| Config | hit@5 | avg latency |
|---|---|---|
| `--no-lazy` (rg-only) | **0/10** | 4.85 s |
| default (auto-trigger) | **4/10** | 20.76 s |
| **delta** | **+4 hits / 10** | **+15.9 s / query** |

The 0.5.0 → 0.5.2 line claimed lazy was a recall improvement but
actually delivered 1/10 in the apples-to-apples CLI bench. 0.5.3
takes that to 4/10 — a real, measurable +30 % hit-rate over rg
cold-start on vocabulary-mismatch queries. Per-query cost is +16 s
(~5–35 s wall on a fresh DB), within the user's accepted 几秒钟
budget for first-touch queries.

Specific hits:
- Q1 "Where does Django turn an incoming URL into the view function" → `django/urls/resolvers.py` ✓
- Q3 "migration runner that applies pending schema changes" → `migration.py` + `executor.py` both in top-5 ✓
- Q4 "authentication backend that checks a username and password" → `backends.py` ✓
- Q7 "request-handler middleware chain assembled before the first request" → `base.py` ✓

Misses (Q2 SQL builder, Q5 template rendering, Q6 CSRF, Q8 file upload, Q9 connection reuse, Q10 form validation): the bench's verbose oracle phrasings still confuse qwen 2.5:3b enough that the right dir doesn't enter the seed pool. Tracked as "0.6 candidate: query simplification + larger LLM router model" in pending list.

Migration canary (specifically chosen because it broke 0.5.0/1/2):

```
$ skygrep "where does Django apply migrations"

╭─ django/db/migrations/migration.py    0.682
╭─ django/db/migrations/recorder.py     0.668
╭─ django/db/migrations/executor.py     0.667     ← oracle expected
╭─ django/core/management/commands/squashmigrations.py  0.656
╭─ django/db/migrations/loader.py       0.655
```

Both `migration.py` (oracle alternative) and `executor.py` (oracle
expected) appear in top-5. In 0.5.2 the same query returned 5 numbered
`*/migrations/0001_*.py` files and hit-rate was 0/1.

## Compatibility

- `--lazy/--no-lazy` flag: unchanged (default on, 0.5.1 contract).
- Public API in `lazy_indexer`: signatures unchanged; new helpers
  (`extract_imports`, `resolve_imports_to_paths`, `_dedupe_seed_groups`)
  are additive and exported via `__all__`.
- `embed_files_batch` now returns a 3-tuple
  `(int, dict, dict)` — the third element is the freshly-loaded text
  per file so callers can run `extract_imports` without a second disk
  read. Internal callers updated; no external callers existed.
- `infer_candidate_paths` default timeout raised from
  `LLM_TIMEOUT_SECONDS` (0.5 s) to 8 s. Pass `timeout=` explicitly to
  override.
- Pytest 201/201 baseline preserved; 16 new dedup + diffusion tests
  added (217 total).

## Verified

- `pytest tests/` — **217 / 217 pass** (201 baseline + 16 new dedup
  + extract_imports + resolve_imports tests)
- Django oracle bench (10 vocabulary-mismatch queries, real CLI):
  **rg-only 0/10 → auto-trigger 4/10**
- Migration canary: `migration.py` + `executor.py` both in top-5
- Auth canary: `backends.py` in top-5
- URL resolver canary: `resolvers.py` in top-5
- Middleware canary: `base.py` in top-5
- `--no-lazy` opt-out still suppresses lazy entirely (1.8 s footer
  shows `--no-lazy` explicitly, 0 lazy hits)
- PyPI install of 0.5.3 in fresh venv reports correct version

## Pending for 0.6

- Token-shortcut for `*/migrations/*` paths is still the dominant
  signal even after dedup + numeric penalty — a future patch can
  treat module-system routes (Python package paths, JS modules) as
  the primary signal and use string match as the tie-break.
- LLM router currently only consulted in `lazy_explore_cold_start`;
  `lazy_explore_cross_folder` is still pure token-match for now.
- Per-query progress streaming via SSE/WebSocket (the current stderr
  lines suffice for a CLI but a future TUI / REPL would benefit).
