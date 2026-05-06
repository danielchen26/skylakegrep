# skylakegrep 0.5.7 — hot-fix: cross-folder lazy was silently failing in worker thread

0.5.6 introduced the parallel proactive umbrella architecture and
fixed cascade's "SQLite objects can only be used in their creating
thread" error by opening a dedicated SQLite connection inside the
cascade worker thread. **The same fix was missed for the two
cross-folder lazy paths**: cold + wrong-folder branch
(`_run_cross` in `cli.py`) and warm + low-confidence branch (the
post-cascade `_LZ.lazy_explore_cross_folder` invocation).

Both passed the main-thread `conn` into a `ThreadPoolExecutor`
worker. SQLite raised  
`SQLite objects created in a thread can only be used in that
same thread. The object was created in thread id A and this
is thread id B`  
inside `lazy_explore_cross_folder`'s embedding-cache lookup. The
exception was caught by the surrounding `try / except` and
silently turned into "0 results" — the user saw cross-folder
return empty even when the answer existed in a sibling repo.

## Why it didn't show up in 0.5.6 bench

The proactive umbrella's parallel-tier architecture saved us. The
0.5.6 bench (10 Django oracle queries) preserves 4/10 hit-rate
because:

- The hits come from cascade running inside the cwd-indexed
  Django repo (cascade tier).
- `filename_extend` (proactive) handles file-name queries.
- Cross-folder lazy was *additional*, not load-bearing for the
  bench.

The user-reported symptom was visible only on the wrong-path
demo:  
`cd /tmp/skg-empty-cwd && SKYGREP_PROACTIVE_DIRS=/tmp/oss-bench
 skygrep "django ORM query builder"`  
which printed `lazy cross-folder failed: SQLite objects created
in a thread can only be used...` to stderr alongside an answer
from `filename_extend`. The umbrella pattern delivered an answer
in 1 s anyway (proactive tier), but the lazy semantic tier was
a dead path.

## What changed in 0.5.7

`cli.py`:

1. **`_run_cwd` worker** (cold-start branch): now opens
   `init_db(db_path)` inside the worker, passes that conn to
   `lazy_explore_cold_start`, closes in `finally`.
2. **`_run_cross` worker** (cold + wrong-folder branch): same
   pattern — own `init_db(db_path)`, own close.
3. **`_warm_cross_in_worker`** (warm + low-confidence cross-
   folder): same pattern. Defined as a closure that opens its
   own conn, runs `lazy_explore_cross_folder`, closes; the
   outer `ThreadPoolExecutor` submits the closure rather than
   the bare `lazy_explore_cross_folder` with main-thread conn.

Identical pattern to the 0.5.6 cascade-in-worker fix; this
release just applies it consistently across all three
ThreadPool worker call sites.

## Verified

- Wrong-path demo: `cd` into empty dir,
  `SKYGREP_PROACTIVE_DIRS=/tmp/oss-bench`, query
  "django ORM query builder" → **wall 1 s**, cross-folder lazy
  returns 5 cosine-ranked Django files (no SQLite error in
  stderr).
- Hard bench (10 Django oracle queries, real CLI, fresh DB):
  rg-only **0/10** → auto-trigger **4/10** (preserved from
  0.5.3 / 0.5.6).
- Pytest 217 / 217.

## Compatibility

- Public API unchanged. `cli.py` internal closures only.
- Thread-local SQLite connections are ephemeral; no schema
  changes, no index format change, no behavioural change for
  callers who don't go through cross-folder.
- 0.5.6-built indexes work without re-indexing.

## RED LINE checklist

Per `docs/EXISTING-INTELLIGENCE-LAYERS.md`:

1. **Layer**: Tier 3 proactive umbrella subprocesses
   (`lazy_explore_cwd`, `lazy_explore_cross_folder` worker
   wrappers).
2. **Interactions**: SQLite via `init_db(db_path)`; same
   `embedder` instance inside each worker; ThreadPoolExecutor.
3. **Action**: PRESERVE — 0.5.7 makes the worker pattern match
   what 0.5.6 already did for cascade. Behaviour returns to
   what was intended in 0.5.6.
4. **Promote/demote history**: N/A — this is a defect fix.
5. **New hyperparameters**: 0.
6. **Bench through real CLI**: ✓ (10 Django oracle, real CLI).
7. **Telemetry footer**: unchanged.
8. **Hard timeout**: cross-folder 8 s, cascade 30 s — both
   preserved.
9. **Personal-data risk**: none — generic placeholder query in
   demo.
10. **Bench receipts**: 4/10 preserved; wrong-path wall 1 s.

## Pending for 0.6

Unchanged from 0.5.6:

- σ-noise-band → structural-fallback chain (symbol_channel +
  filename_shortcut + lexical_shortcut RRF instead of cross-
  encoder rerank when σ-gap is in the noise band).
- True streaming inside Ollama batch embed (split 25 → 5 + 20).
- Structured per-tier observability for ralph / autopilot.
