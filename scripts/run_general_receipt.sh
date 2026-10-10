#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Produce a General Benchmark v2 receipt on this machine and push it to a
# review branch. Needs: git, python3 >= 3.9, ripgrep, and Ollama with bge-m3
# (Apple-silicon Metal or another accelerator: CPU-only machines cannot index
# React / Django / Spring in reasonable time).
#
#   curl -fsSL https://raw.githubusercontent.com/danielchen26/skylakegrep/master/scripts/run_general_receipt.sh | bash
#
# Everything happens in a fresh clone under ~/.cache/skygrep-receipt, so an
# existing checkout is never touched. Takes several hours; keep the machine on
# power. Only the privacy-scanned receipt JSON is committed; logs stay local.
#
# Environment overrides: SKYGREP_RECEIPT_HOME, SKYGREP_RECEIPT_POLICIES,
# SKYGREP_RECEIPT_TRIALS, SKYGREP_RECEIPT_REPOS (space-separated).

set -euo pipefail

REPO_URL="https://github.com/danielchen26/skylakegrep.git"
HOME_DIR="${SKYGREP_RECEIPT_HOME:-$HOME/.cache/skygrep-receipt}"
POLICIES="${SKYGREP_RECEIPT_POLICIES:-skygrep-first skygrep-slim rg-agent rg-only}"
TRIALS="${SKYGREP_RECEIPT_TRIALS:-3}"
REPOS="${SKYGREP_RECEIPT_REPOS:-}"
STAMP="$(date +%Y-%m-%d)"
SRC="$HOME_DIR/src"
OSS="$HOME_DIR/oss"
LOG="$HOME_DIR/run-$STAMP.log"
REPORT_NAME="general-v2-$STAMP-slim.json"

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

say "Checking prerequisites"
command -v git >/dev/null || die "git not found"
command -v python3 >/dev/null || die "python3 not found"
command -v rg >/dev/null || die "ripgrep not found (macOS: brew install ripgrep)"
command -v ollama >/dev/null || die "ollama not found (https://ollama.com/download)"
if ! curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  (ollama serve >/dev/null 2>&1 &)
  for _ in $(seq 1 30); do
    curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
    sleep 2
  done
fi
curl -fsS http://127.0.0.1:11434/api/tags >/dev/null 2>&1 || die "Ollama server is not reachable"
ollama pull bge-m3

say "Fresh clone of master in $SRC"
mkdir -p "$HOME_DIR"
rm -rf "$SRC"
git clone -q "$REPO_URL" "$SRC"
cd "$SRC"
python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -e ".[benchmark]"

policy_args=()
for p in $POLICIES; do policy_args+=(--policy "$p"); done
repo_args=()
for r in $REPOS; do repo_args+=(--repo "$r"); done
keep_awake=()
command -v caffeinate >/dev/null && keep_awake=(caffeinate -dims)

say "Running the benchmark (log: $LOG). This takes several hours."
${keep_awake[@]+"${keep_awake[@]}"} .venv/bin/python benchmarks/universal_closed_loop_benchmark.py \
  --oss-root "$OSS" --prepare --refresh-index --reset-index \
  --index-timeout 18000 --trials "$TRIALS" --tokenizer tiktoken \
  "${policy_args[@]}" ${repo_args[@]+"${repo_args[@]}"} \
  --report "benchmarks/reports/$REPORT_NAME" > "$LOG" 2>&1 \
  || die "benchmark failed; see $LOG"

say "Privacy scan"
.venv/bin/python scripts/privacy_release_scan.py "benchmarks/reports/$REPORT_NAME"

say "Summary"
.venv/bin/python benchmarks/compare_policies.py "benchmarks/reports/$REPORT_NAME" | tee -a "$LOG"

say "Pushing the receipt to branch bench/receipt-$STAMP"
git checkout -q -b "bench/receipt-$STAMP"
git add "benchmarks/reports/$REPORT_NAME"
git commit -q -s -m "bench: General Benchmark v2 receipt $STAMP (${POLICIES// /, })"
git push -q -u origin "bench/receipt-$STAMP"
say "Done. Receipt: benchmarks/reports/$REPORT_NAME on branch bench/receipt-$STAMP"
