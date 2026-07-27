#!/usr/bin/env bash
# Bring up the whole simulator: both cameras, both terminals, and the server.
# Ctrl-C stops everything.
#
#   ./run_sim.sh            # normal run
#   ./run_sim.sh --fresh    # wipe the database first
set -euo pipefail

cd "$(dirname "$0")"

PYTHON="${PYTHON:-.venv/bin/python}"
ENV_NAME="${TRAFIX_ENV:-sim}"
LOG_DIR="logs"

if [[ ! -x "$PYTHON" ]]; then
  echo "no interpreter at $PYTHON — create one with:" >&2
  echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

if [[ "${1:-}" == "--fresh" ]]; then
  rm -f trafix.db trafix.db-wal trafix.db-shm
  echo "database wiped"
fi

if ! "$PYTHON" - <<'EOF'
import socket, sys
from trafix.config import load_config
config = load_config()
try:
    socket.create_connection((config.broker.host, config.broker.port), timeout=2).close()
except OSError:
    sys.exit(1)
EOF
then
  echo "no MQTT broker reachable. Start it with:" >&2
  echo "  docker compose up -d mosquitto" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
PIDS=()

cleanup() {
  echo
  echo "stopping..."
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

start() {
  local name="$1"; shift
  "$PYTHON" -m "$@" --env "$ENV_NAME" >"$LOG_DIR/$name.log" 2>&1 &
  PIDS+=($!)
  echo "  started $name (pid ${PIDS[-1]}, log $LOG_DIR/$name.log)"
}

echo "starting simulator (env=$ENV_NAME)"
start lpr_in      mocks.lpr_mock --device lpr_in
start lpr_out     mocks.lpr_mock --device lpr_out
sleep 2
start manless_in  mocks.manless_mock --lane in
start manless_out mocks.manless_mock --lane out
sleep 1
start server      trafix.server.app

echo
echo "ready. In another terminal:"
echo "  $PYTHON -m cli.trafix status"
echo "  $PYTHON -m cli.trafix press --lane in"
echo "  $PYTHON -m cli.trafix txn list"
echo
echo "following server log — Ctrl-C to stop everything"
echo
tail -f "$LOG_DIR/server.log" "$LOG_DIR/manless_in.log" "$LOG_DIR/manless_out.log"
