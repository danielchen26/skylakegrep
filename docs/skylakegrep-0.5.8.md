# skylakegrep 0.5.8 — `--explain`: every result tells you why it matched

The single biggest UX gap in 0.5.7 was that skygrep could surface a
result with a 0.522 cosine score and a `cosine-escalated-rerank`
cascade lane and a `qwen2.5:3b` LLM router decision, and the user
only saw the score. The signal that *would* explain the rest was
already in the pipeline; we just threw it away at render time.

0.5.8 stops throwing it away.

## What's new

### `--explain` / `-x` — three layers of "why"

Pass `--explain` (or `-x`) and skygrep prints:

1. **Router rationale** at the top of the output:
   `🧭 router: <intent> · primary_token=<token> · conf=<c> · source=<llm|fallback-rules|fallback-mixed>`
   plus a one-sentence reason from the LLM router itself.
2. **Per-result `via:` line** under each result header showing
   which channel(s) contributed: `cosine cascade · score=…` /
   `filename-lookup · token "…" · score=…` /
   `cosine #1 ⊕ symbol #2 (RRF)` / `ripgrep · lex=…` plus matched
   symbol terms when the symbol channel hit.
3. **`🛤  cascade lane:` summary** at the bottom showing which
   retrieval lane answered with σ-adaptive evidence
   (`gap=… , tau=…`).

```
$ skygrep -x "find pyproject.toml in this repo"

🧭 router: filename · primary_token="pyproject.toml" · conf=0.95 · source=llm
   reason: "user is looking for a specific file by name in the repo"

╭─ pyproject.toml ────────────────────────────────── [toml]  1.000
│ via: filename-lookup · token "pyproject.toml" · score=1.000
│
│ size:  1.0 KB    modified: 2026-05-06 16:51    type: toml
╰──────────────────────────────────────────────────────────────────

🛤  cascade lane: cosine-cheap (gap=0.037, tau=0.016)
```

Off by default — existing UX is byte-identical to 0.5.7. No new
model calls, no extra retrieval, no new hyperparameters. Every signal
the renderer emits was already on the result dict (`cosine_rank`,
`symbol_rank`, `symbol_channel_terms`, `fallback`, `score`,
`fused_score`, `decision.intent`, `decision.primary_token`,
`decision.reason`, `cascade_telemetry.path / .gap / .tau`).

### Bonus: Ollama autostart

If `ollama serve` is not running but the `ollama` binary is on PATH,
skygrep now autostarts it in the background, polls until it's
reachable (5 s budget, env-tunable), and prints exactly one
status line so the user knows what happened:

```
🔧 Ollama not running — starting in background…
✓ Ollama up after 1.4 s
```

Idempotent within a process (won't double-spawn). Disable via
`SKYGREP_AUTOSTART_OLLAMA=0`. Override budget via
`SKYGREP_OLLAMA_AUTOSTART_TIMEOUT`.

## Two latent LLM-router bugs fixed along the way

While building `--explain` we discovered that on this same machine
the `🧭 router:` line was always reporting `source=fallback-rules`
even with Ollama running and `qwen2.5:3b` loaded. Investigation
turned up two pre-existing defects:

1. **`keep_alive` was sent as the string `"-1"`.** Recent Ollama
   versions parse that field as a duration string and reject
   bare-number strings with `time: missing unit in duration "-1"`
   (HTTP 400). The fix routes through the existing
   `_coerce_keep_alive()` helper (already used by
   `embeddings.py`) which converts numeric strings to
   integers. Same defect was already worked around in the
   HyDE LLM call — now consistent across all router LLM call sites.
2. **`LLM_TIMEOUT_SECONDS` defaulted to `0.5`.** Cold qwen2.5:3b
   needs ~3 s to respond with `format=json + num_predict=256`;
   warm with `keep_alive=-1` (now actually working) is ~50–100 ms.
   The 0.5 s default timed out 100 % of cold calls before the
   model could answer. Bumped to 8 s. Subsequent warm calls are
   well under the new ceiling.

Both bugs were silently masking the LLM router; from a user's
perspective, intent classification was always degrading to the rule-
based v0.14.0 fallback. With these fixes the LLM router actually
fires, and `--explain` prints `source=llm` with a real LLM-authored
reason instead of `source=fallback-rules`.

## ES comparison section on README + Pages

We added a dedicated **"How skylakegrep differs from Elasticsearch"**
section to both the README (collapsed `<details>` block) and the
docs/index.html homepage (`#vs-elasticsearch` section, in the left-
rail nav). 13-row capability matrix; honest about where ES wins
(scale, multi-tenant, aggregations) and where skylakegrep wins
(zero-ops, semantic by default, intent understanding, code AST,
cold-start, why-this-matched).

## Verified

- **207/207 unit tests pass** (full pytest suite, excluding the
  slow OSS bench fixture).
- **Real CLI tests** across all paths: default UX, `--json`,
  `--no-llm-router`, `--no-cascade`, `--no-rerank`,
  wrong-folder lazy_cross_folder, `--hyde`.
- **Head-to-head vs 0.5.7 PyPI** on identical query: same paths,
  same scores. Zero regression when `--explain` is off.
- **Autostart logic** verified via fake-port unit test (echoes
  the four expected status branches: not-installed / spawning /
  up / timed-out).

## Compatibility

- Public API unchanged. `--explain` is opt-in.
- JSON output (`--json`) keys unchanged: `path`, `start_line`,
  `end_line`, `language`, `score`, `snippet`. The `explain` field
  is internal-only, attached to the in-memory result dict and
  rendered by the terminal renderer, not serialised to JSON.
- 0.5.7-built indexes work without re-indexing.
- `LLM_TIMEOUT_SECONDS` default change (0.5 s → 8 s) is a behaviour
  change for one path only: the LLM router HTTP call. Override via
  `SKYGREP_LLM_ROUTER_TIMEOUT_SECONDS`.

## RED LINE checklist

Per `docs/EXISTING-INTELLIGENCE-LAYERS.md`:

1. **Layer**: render layer (`render.py:render_terminal_result`) +
   CLI orchestration (`cli.py:_build_explain_string` /
   `_attach_explain` / `_format_router_explain` /
   `_format_lane_explain`). Plus `bootstrap.py:try_autostart_ollama`.
   Plus router LLM-call payload coercion.
2. **Interactions**: read-only on signals already populated by
   the cosine cascade, symbol_channel RRF fusion, filename_shortcut,
   lexical_shortcut, and llm_router. No new reads, no new writes.
3. **Action**: ADD (rendering surface) + FIX (latent router bugs).
   No retrieval logic touched.
4. **Promote/demote history**: N/A.
5. **New hyperparameters**: 0 user-facing. The `LLM_TIMEOUT_SECONDS`
   default change is restoring a working timeout, not introducing
   a new tuning knob.
6. **Bench through real CLI**: ✓.
7. **Telemetry footer**: unchanged.
8. **Hard timeout**: cross-folder 8 s, cascade 30 s — both preserved.
   New: Ollama autostart 5 s deadline.
9. **Personal-data risk**: none — generic example queries
   (`pyproject.toml` etc.) in code + docs.
10. **Bench receipts**: byte-identical to 0.5.7 with `--explain` off;
    207/207 unit tests pass; full E2E test plan executed.

## Pending for 0.6

Unchanged from 0.5.7:

- σ-noise-band → structural-fallback chain (symbol_channel +
  filename_shortcut + lexical_shortcut RRF instead of cross-
  encoder rerank when σ-gap is in the noise band).
- True streaming inside Ollama batch embed (split 25 → 5 + 20).
- Structured per-tier observability for ralph / autopilot.
- Optional: expose `explain` field in `--json` output for
  programmatic consumers.
