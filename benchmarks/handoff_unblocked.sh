#!/usr/bin/env bash
# Run the two experiments that a network blocking huggingface.co cannot run.
#
# Everything else in this branch was measured on a machine where huggingface.co
# is blocked by category. Two questions survived that block:
#
#   1. Is hit@1 21.7% a scoring-resolution problem or a candidate-generation
#      problem?  ->  skygrep-first vs skygrep-rerank
#   2. How does skylakegrep rank against a competitor that also ranks?
#      ->  ck-sem / ck-hybrid
#
# Both need the same thing: a machine that can reach huggingface.co. One trip,
# two answers.
#
# Usage:
#   bash benchmarks/handoff_unblocked.sh /path/to/oss-root [repo ...]
#
# With no repo arguments it runs the full pinned six. Start with `cobra` for a
# ten-minute smoke check before committing to the full run.
set -euo pipefail

OSS_ROOT="${1:?usage: handoff_unblocked.sh <oss-root> [repo ...]}"
shift || true
REPOS=("$@")
if [ ${#REPOS[@]} -eq 0 ]; then
  REPOS=(cobra tokio vite spring-framework django react)
fi

PY="${PY:-./.venv/bin/python}"
OUT="${OUT:-benchmarks/reports/unblocked-$(date -u +%Y-%m-%d)}"
mkdir -p "$OSS_ROOT" "$OUT"
LOG="$OUT/progress.log"

say() { echo "[$(date -u +%T)] $*" | tee -a "$LOG"; }

say "=== preflight: is this machine actually unblocked? ==="
$PY -m benchmarks.dependency_preflight --out "$OUT/dependency-preflight.json" | tee -a "$LOG"
if grep -q '"measured_status": "blocked_by_policy"' "$OUT/dependency-preflight.json"; then
  say "!! something is still blocked here. Read the receipt above before trusting any"
  say "!! number that follows: an arm that cannot fetch its model produces a miss,"
  say "!! not an honest zero. Stopping."
  exit 1
fi
say "preflight clean"

say "=== dependencies ==="
$PY -m pip install -q -e '.[rerank,benchmark]'
command -v ck >/dev/null || { say "installing ck"; cargo install ck-search; }
command -v ollama >/dev/null || { say "!! ollama is required for the skygrep arms"; exit 1; }
ollama pull bge-m3
ollama pull qwen2.5:3b
say "ck $(ck --version 2>&1 | head -1)"

# Warm ck's own index once per repo so its first timed query is not paying for
# an index build the other arms already paid for outside the timer.
for repo in "${REPOS[@]}"; do
  case "$repo" in
    cobra) sub=cobra ;; *) sub="$repo" ;;
  esac
  if [ -d "$OSS_ROOT/$sub" ]; then
    say "warming ck index for $repo"
    ( cd "$OSS_ROOT/$sub" && ck --index . >/dev/null 2>&1 ) || say "  ck index failed for $repo"
  fi
done

say "=== four arms, sequential (concurrency poisons every latency figure) ==="
for repo in "${REPOS[@]}"; do
  receipt="$OUT/gv2-$repo.json"
  [ -s "$receipt" ] && { say "$repo done already, skipping"; continue; }
  say "$repo START"
  start=$(date +%s)
  $PY -m benchmarks.universal_closed_loop_benchmark \
      --repo "$repo" \
      --oss-root "$OSS_ROOT" \
      --prepare \
      --policy skygrep-first \
      --policy skygrep-rerank \
      --policy ck-sem \
      --policy rg-only \
      --trials 3 \
      --tokenizer tiktoken \
      --refresh-index --reset-index \
      --index-timeout 18000 \
      --min-general-tasks 3 --min-general-repos 1 \
      --report "$receipt" > "$OUT/$repo.stdout" 2> "$OUT/$repo.stderr"
  say "$repo DONE in $(( $(date +%s) - start ))s"
  $PY - "$receipt" <<'PYEOF' | tee -a "$LOG"
import json, sys
totals = json.load(open(sys.argv[1]))["aggregate"]["totals"]
for arm, v in totals.items():
    print(f"    {arm:<16} cov={v['path_coverage_pct']:<6} mrr={str(v['mrr']):<7}"
          f" hit@1={str(v['hit_at_1_pct']):<6} hit@3={str(v['hit_at_3_pct']):<6}"
          f" quality={v['work_quality_pct']:<6} tokens={v['context_tokens']}")
PYEOF
done

say "=== merge ==="
$PY -m benchmarks.merge_general_reports "$OUT"/gv2-*.json --output "$OUT/merged.json" \
  && say "merged -> $OUT/merged.json" \
  || say "merge skipped (needs all six pinned repos); per-repo receipts remain in $OUT"

say "=== the two answers ==="
$PY - "$OUT" <<'PYEOF' | tee -a "$LOG"
import glob, json, sys, os
files = sorted(glob.glob(os.path.join(sys.argv[1], "gv2-*.json")))
acc: dict[str, dict[str, list[float]]] = {}
for f in files:
    for arm, v in json.load(open(f))["aggregate"]["totals"].items():
        row = acc.setdefault(arm, {"mrr": [], "h1": [], "h3": [], "tok": []})
        if v.get("mrr") is not None:
            row["mrr"].append(v["mrr"])
            row["h1"].append(v["hit_at_1_pct"])
            row["h3"].append(v["hit_at_3_pct"])
        row["tok"].append(v["context_tokens"])
def mean(xs):
    return round(sum(xs) / len(xs), 3) if xs else None
print(f"\n  {'arm':<16}{'MRR':>8}{'hit@1':>8}{'hit@3':>8}{'tokens':>14}")
for arm, row in acc.items():
    print(f"  {arm:<16}{str(mean(row['mrr'])):>8}{str(mean(row['h1'])):>8}"
          f"{str(mean(row['h3'])):>8}{sum(row['tok']):>14}")
print("""
  Read it like this:
    skygrep-rerank >> skygrep-first  ->  ranking was a scoring-resolution
                                        problem; the reranker is the fix
    skygrep-rerank ~= skygrep-first  ->  it is candidate generation; the fix is
                                        a lexical/BM25 lane, not a better scorer
    ck-sem         >> both           ->  a real ranking gap to close, and the
                                        first honest ranking comparison this
                                        project has ever had
    ck-sem         << both           ->  recall and ranking both favour us, and
                                        the benchmark is now credible because a
                                        real competitor was in it
""")
PYEOF
say "=== end ==="
