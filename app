#!/usr/bin/env bash
# voice-roleplay local launcher (PRD §14.3, PROTOCOL §11).
#
#   ./app setup [--yes]    install runtimes, web build and pinned models (asks first; --yes = no prompt)
#   ./app doctor [--full]  check hardware, runtimes, model hashes, ports, voices, offline readiness
#   ./app start            start gateway + ASR/TTS/LLM (+ optional pronunciation) workers, wait until ready, print the URL
#   ./app stop             stop gracefully (jobs cancelled, workers stopped), clean up leftovers
#   ./app benchmark [...]  run benchmarks/run.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT/var/run"
LOG_DIR="$ROOT/var/log"
PORT="${VR_GATEWAY_PORT:-8710}"
URL="http://127.0.0.1:$PORT"
GATEWAY_PY="$ROOT/services/gateway/.venv/bin/python"
TOOL="$ROOT/scripts/app_tool.py"

# Homebrew (llama-server) and uv's default install dir, whatever shell started us.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
PYTHON="${PYTHON:-$(command -v python3.12 || command -v python3)}"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

pid_alive() { [ -n "${1:-}" ] && kill -0 "$1" 2>/dev/null; }
read_pid() { [ -f "$RUN_DIR/$1.pid" ] && head -n1 "$RUN_DIR/$1.pid" || true; }

# Only signal a pid from a pidfile if it still runs the program we started there (pids get reused).
pid_is() {
  local pid=$1 pattern=$2
  pid_alive "$pid" && ps -o command= -p "$pid" 2>/dev/null | grep -q -- "$pattern"
}

cmd_setup() {
  local yes=0
  [ "${1:-}" = "--yes" ] && yes=1
  cat <<'EOF'
./app setup will:
  1. create Python environments with uv from the lock files (services/gateway, workers/asr, workers/tts,
     and the optional pronunciation worker workers/pronunciation, which compiles pyworld: needs Xcode Command Line Tools)
  2. clone vendor/CosyVoice at the pinned commit (workers/tts/setup.sh)
  3. install and build the web app (npm ci && npm run build in apps/web)
  4. download the model files listed in models.lock.json from Hugging Face (and CMUdict from GitHub) at pinned revisions
     (Qwen3-ASR-0.6B ~1.8 GB, Fun-CosyVoice3-0.5B-2512 ~5 GB, its MLX fp16 conversion ~1.7 GB,
      Qwen3-4B-Instruct-2507 Q4_K_M GGUF ~2.4 GB; for pronunciation analysis Qwen3-ForcedAligner-0.6B ~1.8 GB,
      wav2vec2-lv-60-espeak-cv-ft ~1.3 GB, CMUdict ~3.6 MB)
     and verify every file's SHA-256
This uses the network (PyPI, GitHub, npm, Hugging Face) and about 25 GB of disk (models ~18 GB, environments ~5.5 GB). After setup the app runs offline.
EOF
  if [ $yes -ne 1 ]; then
    [ -t 0 ] || die "no terminal to ask for consent; re-run with --yes to agree non-interactively"
    read -r -p "Continue? [y/N] " answer
    case "$answer" in y|Y|yes|YES) ;; *) say "Cancelled. Nothing was installed."; exit 1 ;; esac
  fi

  command -v uv >/dev/null || die "uv not found (install: https://docs.astral.sh/uv/)"
  command -v git >/dev/null || die "git not found"
  command -v llama-server >/dev/null || die "llama-server not found (brew install llama.cpp)"
  if ! command -v npm >/dev/null && [ -s "$HOME/.nvm/nvm.sh" ]; then
    # shellcheck disable=SC1091
    set +u; . "$HOME/.nvm/nvm.sh" >/dev/null; set -u
  fi
  command -v npm >/dev/null || die "npm not found (install Node.js 22)"

  say "== [1/4] Python environments (uv sync --frozen)"
  (cd "$ROOT/services/gateway" && uv sync --frozen)
  (cd "$ROOT/workers/asr" && uv sync --frozen)
  # Optional: without it recorded practice works and reports pronunciation analysis as unavailable.
  if ! "$ROOT/workers/pronunciation/setup.sh"; then
    say "warning: the optional pronunciation worker was not installed (see the error above); continuing without it"
  fi
  say "== [2/4] TTS worker: vendor/CosyVoice + environment"
  "$ROOT/workers/tts/setup.sh"
  say "== [3/4] Web app"
  (cd "$ROOT/apps/web" && npm ci && npm run build)
  say "== [4/4] Models (pinned revisions, then SHA-256 check)"
  HF_HUB_DISABLE_TELEMETRY=1 "$ROOT/workers/asr/.venv/bin/python" "$TOOL" download
  say ""
  say "Setup complete. Next: ./app doctor, then ./app start"
}

cmd_doctor() {
  "$PYTHON" "$TOOL" doctor "$@"
}

cmd_start() {
  local pid
  pid=$(read_pid gateway)
  if pid_is "$pid" vr_gateway; then
    say "Already running (pid $pid): $URL"
    return 0
  fi
  say "Checking installation..."
  if ! "$PYTHON" "$TOOL" preflight; then
    die "cannot start (fix the items above; run ./app setup for missing files, ./app stop for a previous run)"
  fi
  mkdir -p "$RUN_DIR" "$LOG_DIR"
  say "Starting gateway and workers (logs: var/log/)..."
  (cd "$ROOT/services/gateway" && exec nohup "$GATEWAY_PY" -m vr_gateway --manage-workers \
      >>"$LOG_DIR/gateway.log" 2>&1 </dev/null) &
  pid=$!
  echo "$pid" >"$RUN_DIR/gateway.pid"
  say "Waiting for models to load (first start can take a few minutes; Ctrl-C stops everything)..."
  trap 'say ""; say "Interrupted; stopping."; cmd_stop; exit 130' INT TERM
  if ! "$PYTHON" "$TOOL" wait-ready "$pid" "${VR_START_TIMEOUT_S:-900}"; then
    say "Start failed; stopping what was started."
    cmd_stop >/dev/null || true
    die "see var/log/gateway.log, var/log/asr.log, var/log/tts.log, var/log/llm.log (and var/log/pron.log)"
  fi
  trap - INT TERM
  say ""
  say "Ready: $URL"
}

stop_worker() {
  local name=$1 pattern=$2 pid
  pid=$(read_pid "$name")
  if pid_is "$pid" "$pattern"; then
    say "  stopping leftover $name worker (pid $pid)"
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do pid_alive "$pid" || break; sleep 0.5; done
    if pid_alive "$pid"; then kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true; fi
  fi
  rm -f "$RUN_DIR/$name.pid"
}

cmd_stop() {
  local pid
  pid=$(read_pid gateway)
  if pid_is "$pid" vr_gateway; then
    say "Stopping gateway (pid $pid): cancelling jobs, closing sessions, stopping workers..."
    kill -TERM "$pid"
    for _ in $(seq 1 60); do pid_alive "$pid" || break; sleep 0.5; done
    if pid_alive "$pid"; then
      say "  gateway did not exit in 30 s; killing it"
      kill -KILL "$pid" 2>/dev/null || true
    fi
  else
    say "Gateway not running."
  fi
  rm -f "$RUN_DIR/gateway.pid"
  stop_worker asr asr_worker
  stop_worker tts tts_worker
  stop_worker llm llama-server
  stop_worker pron pron_worker
  say "Stopped."
}

cmd_benchmark() {
  exec "$ROOT/benchmarks/run.sh" "$@"
}

case "${1:-}" in
  setup) shift; cmd_setup "$@" ;;
  doctor) shift; cmd_doctor "$@" ;;
  start) shift; cmd_start "$@" ;;
  stop) shift; cmd_stop "$@" ;;
  benchmark) shift; cmd_benchmark "$@" ;;
  *) sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
