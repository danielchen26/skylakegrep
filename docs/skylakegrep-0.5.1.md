# skylakegrep 0.5.1 — auto-trigger lazy on cold-start (no flag needed)

0.5.0 shipped a `--lazy` flag and called the architecture done. Wrong.
The whole premise of lazy is that **the user does not know whether
they're in the right folder, or whether their query happens to align
with the code's vocabulary** — so the user cannot be expected to
decide whether to add a flag. 0.5.1 flips lazy to auto-trigger.

## What changed

`skygrep "<query>"` (default, no flag) on a never-indexed project now
auto-decides between three tiers:

```
ripgrep cold-start           ←  ~100 ms, always runs
  ↓ if rg paths cover ≥ 2 distinct query tokens (camel/snake split)
  →  return rg-only          ←  user gets answer in 100 ms
  ↓ otherwise (vocabulary mismatch / wrong folder)
  →  also fire lazy semantic ←  ~5 s wait, LLM-routed entry-points
                                 + bge-m3 batch embed, σ-validated
                                 → merge with rg → return both
```

`--no-lazy` opts out (e.g. for benchmarking pure rg). `--lazy` is
now the default (kept for explicit testing).

The "rg is strong" gate is intentionally strict: it requires ≥ 2
distinct query tokens (after PascalCase / snake_case splitting) to
land in result paths. A single common token like `schema` matching
five unrelated `schema.py` files in different backends does **not**
count as strong — that's the vocabulary-mismatch trap that fooled
0.5.0's earlier opt-in design.

## Verified end-to-end on a fresh, never-indexed Django checkout

```
$ skygrep "cache invalidation strategy"

╭─ django/middleware/cache.py                                       0.537
╭─ django/contrib/sessions/backends/cached_db.py                    0.518
╭─ django/templatetags/cache.py                                     0.514
╭─ django/core/cache/backends/redis.py                              0.514
╭─ django/template/loaders/cached.py                                0.509

[8.144 s · ripgrep cold-start + lazy auto (σ=0.010, conf=medium,
 25new/0cached) · intent=lexical · 0 filename + 5 rg + 5 lazy ·
 index building in background]
```

Six scenarios from CLI (not python API) on `/tmp/oss-bench/django`,
fresh DB each time, no `skygrep index .` ever ran:

| # | Query | rg paths align? | Lazy fires? | Latency | Verdict |
|---|---|---|---|---|---|
| A | `ModelForm` | rg returned only `docs/*` | YES | 6.4 s | ✓ lazy delivered `model_forms/*.py` |
| B | `schema synchronization across releases` | only "schema" matched | YES | 8.0 s | ✓ semantic answer |
| C | B with `--no-lazy` | n/a | NO (opt-out) | 0.2 s | ✓ pure rg respected |
| D | `auth backends` | tokens didn't co-occur in path | YES | 9.2 s | ✓ semantic answer |
| E | `cache invalidation strategy` | vocabulary mismatch | YES | 7.3 s | ✓ correct cache modules |
| F | `how does Django render template inheritance` | `render` + `template` aligned | NO (rg strong) | 0.3 s | ✓ instant rg keyword |

Pytest 201/201, no regressions.

## Why this matters

Per the user's vision (and corrected this iteration):

> "本身 lazy 的模式我们就不知道我们是不是在错误的这个 path 下面,
>  所以根本就 user 不可能知道要去加显式这种东西。就是在我们没有
>  搜到或者是没有完全 index 加载的情况下,就是因为我们不知道是不是
>  在错误的文件下或者 path 下提问,我们才会去 trigger 这个所谓的
>  lazy 的模式的对吧。"

0.5.1 implements that exactly: the user types `skygrep "..."`. The
system decides for them. When rg is path-token-strong, user gets
the instant keyword answer. When rg is weak (the case the user can
*never* judge a priori), the system spends 5 s on the lazy semantic
tier so the user gets a correct answer instead of a wrong-fast one.

## Compatibility

- `--lazy` flag still accepted; semantics unchanged from 0.5.0
  (now equivalent to default behavior). Use for explicit testing.
- `--no-lazy` is the new opt-out; needed for benchmarks that want
  pure rg cold-start measurement.
- Indexed projects (post `skygrep index .`) are unaffected — the
  full σ-adaptive cascade still owns the warm-query path.

## What's not yet done (carried from 0.5.0)

- Themed HTML pages for the 0.5.x release notes (only `.md` exists).
- `proactive.filename_extend` integration of `lazy_explore_cross_folder`
  to replace its hardcoded `~/Downloads`/`~/Desktop`/`~/Documents`
  defaults — module API is exposed, wiring deferred to 0.5.2.

## Verified

- `pytest tests/` — 201 / 201 pass
- 6 CLI scenarios on fresh Django (table above)
- `--no-lazy` path measured at 0.2 s, footer correctly reads `--no-lazy`
- `--lazy/--no-lazy` flag round-trips through `click` correctly
