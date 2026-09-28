#!/usr/bin/env bash
# Full-stack benchmark for `./app benchmark` (PRD §15.1, §15.3, AT-14).
#
#   benchmarks/run.sh [--turns N] [--barge-every K] [--session-turns M] [--rec30 R] [--rec120 R] [--name NAME]
#
# Needs a running stack (`./app start`) and the browser test deps (`cd tests/e2e && npm ci && npx playwright install
# chromium`; setup-time network only). Drives Chromium through the real app: N warm realtime turns (default 30;
# PRD §15.3 asks for 200 — use --turns 200), a barge-in on every K-th reply (default 3), then recorded-practice
# jobs of 30 s (R=5) and 120 s (R=2). Writes benchmarks/results/<date>-<machine>.{json,md}.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"

TURNS=30 BARGE=3 SESSION=10 REC30=5 REC120=2 NAME=""
while [ $# -gt 0 ]; do
  case "$1" in
    --turns) TURNS=$2; shift 2 ;;
    --barge-every) BARGE=$2; shift 2 ;;
    --session-turns) SESSION=$2; shift 2 ;;
    --rec30) REC30=$2; shift 2 ;;
    --rec120) REC120=$2; shift 2 ;;
    --name) NAME=$2; shift 2 ;;
    *) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
  esac
done

PYTHON="$(command -v python3.12 || command -v python3)"
URL="http://127.0.0.1:${VR_GATEWAY_PORT:-8710}"
if ! curl -fsS "$URL/api/health" | "$PYTHON" -c 'import json,sys; h=json.load(sys.stdin); sys.exit(0 if h["modes"]["realtime"]["available"] else 1)'; then
  echo "error: the stack is not running or not ready at $URL (run ./app start)" >&2
  exit 1
fi
if ! command -v npx >/dev/null && [ -s "$HOME/.nvm/nvm.sh" ]; then
  # shellcheck disable=SC1091
  set +u; . "$HOME/.nvm/nvm.sh" >/dev/null; set -u
fi
[ -d "$ROOT/tests/e2e/node_modules/@playwright/test" ] || { echo "error: run 'cd tests/e2e && npm ci && npx playwright install chromium' first" >&2; exit 1; }
[ -f "$ROOT/tests/fixtures/audio/e2e_cafe_01.wav" ] || "$ROOT/tests/fixtures/make_fixtures.sh" >/dev/null

machine="$(sysctl -n machdep.cpu.brand_string 2>/dev/null | sed -E 's/^Apple //; s/[^A-Za-z0-9]//g' | tr 'A-Z' 'a-z')"
NAME="${NAME:-$(date +%Y-%m-%d)-${machine:-unknown}}"
OUT="$ROOT/benchmarks/results"
mkdir -p "$OUT" "$ROOT/tests/e2e/.out"
RAW="$ROOT/tests/e2e/.out/bench-$NAME.raw.json"

echo "Benchmark $NAME: $TURNS realtime turns (barge-in every $BARGE), recorded 30 s x $REC30, 120 s x $REC120"
(cd "$ROOT/tests/e2e" && BENCH_TURNS=$TURNS BENCH_BARGE_EVERY=$BARGE BENCH_SESSION_TURNS=$SESSION BENCH_REC30=$REC30 \
  BENCH_REC120=$REC120 BENCH_OUT="$RAW" npx playwright test -c bench.config.ts)
"$PYTHON" "$ROOT/benchmarks/report.py" "$RAW" "$OUT/$NAME"
