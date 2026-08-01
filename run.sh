#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PID_FILE="$ROOT/douyin-downloader.pid"
LOG_DIR="$ROOT/logs"
APP_LOG="$LOG_DIR/douyin-downloader.log"
RUN_LOG="$LOG_DIR/run.out"
CONFIG="${CONFIG:-config.yml}"

# Match scheduler process started from this project (avoid killing unrelated python).
PROC_PATTERN="python([0-9.]*)?[[:space:]]+run\\.py .*--scheduler"

mkdir -p "$LOG_DIR"

is_running() {
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  pgrep -f "$PROC_PATTERN" >/dev/null 2>&1
}

running_pids() {
  local pids=()
  if [[ -f "$PID_FILE" ]]; then
    local pid
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      pids+=("$pid")
    fi
  fi
  local found
  found="$(pgrep -f "$PROC_PATTERN" 2>/dev/null || true)"
  if [[ -n "$found" ]]; then
    while read -r pid; do
      [[ -z "$pid" ]] && continue
      local exists=0
      for x in "${pids[@]:-}"; do
        if [[ "$x" == "$pid" ]]; then
          exists=1
          break
        fi
      done
      if [[ $exists -eq 0 ]]; then
        pids+=("$pid")
      fi
    done <<<"$found"
  fi
  printf '%s\n' "${pids[@]:-}"
}

cmd_status() {
  if is_running; then
    local pid
    pid="$(running_pids | head -1)"
    echo "Running (pid=${pid:-unknown})"
    echo "App log : $APP_LOG"
    echo "Run log : $RUN_LOG"
    echo "Config  : $ROOT/$CONFIG"
  else
    echo "Not running"
  fi
}

cmd_stop() {
  echo "Stopping douyin-downloader..."
  local any=0
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    any=1
    echo "  kill pid=$pid"
    kill "$pid" 2>/dev/null || true
  done < <(running_pids)

  sleep 2

  while read -r pid; do
    [[ -z "$pid" ]] && continue
    echo "  force kill pid=$pid"
    kill -9 "$pid" 2>/dev/null || true
  done < <(running_pids)

  # Also clear any leftover PID file / matching processes
  pkill -f "$PROC_PATTERN" 2>/dev/null || true
  rm -f "$PID_FILE"

  sleep 1
  if is_running; then
    echo "Warning: process still running"
    cmd_status
    exit 1
  fi
  echo "Stopped."
}

cmd_start() {
  if is_running; then
    echo "Already running."
    cmd_status
    exit 0
  fi

  if [[ ! -d "$ROOT/.venv" ]]; then
    echo "ERROR: .venv not found. Create it first:"
    echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
  fi

  if [[ ! -f "$ROOT/$CONFIG" ]]; then
    echo "ERROR: config file not found: $CONFIG"
    echo "  cp config.example.yml config.yml"
    exit 1
  fi

  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"

  echo "Starting douyin-downloader (scheduler)..."
  echo "  Config  : $ROOT/$CONFIG"
  echo "  App log : $APP_LOG"
  echo "  Run log : $RUN_LOG"

  nohup python run.py -c "$CONFIG" "$@" --scheduler >>"$RUN_LOG" 2>&1 &
  echo $! >"$PID_FILE"

  sleep 2
  if is_running; then
    echo "Started OK."
    cmd_status
  else
    echo "Start failed. Check logs:"
    echo "  tail -f $RUN_LOG"
    echo "  tail -f $APP_LOG"
    exit 1
  fi
}

cmd_restart() {
  cmd_stop || true
  sleep 1
  cmd_start "$@"
}

case "${1:-start}" in
  start)
    shift || true
    cmd_start "$@"
    ;;
  stop)
    cmd_stop
    ;;
  restart)
    shift || true
    cmd_restart "$@"
    ;;
  status)
    cmd_status
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status} [extra args for run.py]"
    echo "  CONFIG=config.yml $0 start"
    echo "  $0 start --scheduler-once"
    exit 1
    ;;
esac
