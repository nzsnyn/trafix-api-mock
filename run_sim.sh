#!/usr/bin/env bash
# Bring up the whole simulated site: both LPR units, both gate controllers,
# the API server and the orchestrator. Ctrl-C stops everything.
#
#   ./run_sim.sh            normal run
#   ./run_sim.sh --fresh    drop and recreate the database first
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

check_service() {
  local name="$1" host="$2" port="$3" hint="$4"
  if ! timeout 2 bash -c "cat < /dev/null > /dev/tcp/$host/$port" 2>/dev/null; then
    echo "$name is not reachable at $host:$port" >&2
    echo "  $hint" >&2
    exit 1
  fi
}

check_service "MQTT broker" 127.0.0.1 1883 "docker compose up -d mosquitto"
check_service "PostgreSQL"  127.0.0.1 5433 "docker compose up -d postgres"

if [[ "${1:-}" == "--fresh" ]]; then
  "$PYTHON" - <<'EOF'
from trafix import db
from trafix.config import load_config
config = load_config()
db.init_engine(config.database_url)
db.drop_all()
db.create_all()
with db.session_scope() as session:
    db.seed(session)
print("database recreated and seeded")
EOF
fi

mkdir -p "$LOG_DIR"
PIDS=()

cleanup() {
  echo
  echo "stopping..."
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

start() {
  local name="$1"; shift
  "$PYTHON" -m "$@" --env "$ENV_NAME" >"$LOG_DIR/$name.log" 2>&1 &
  PIDS+=($!)
  echo "  $name (pid ${PIDS[-1]}) -> $LOG_DIR/$name.log"
}

echo "starting the simulated site (env=$ENV_NAME)"

# The LPR units first: the orchestrator checks they answer at startup.
start lpr_in   mocks.lpr_unit --gate 1
start lpr_out  mocks.lpr_unit --gate 2 --publishes-reads
sleep 2

# The relay boards. Gate 2 listens on /GATE/OUT/2, the topic production has
# no equivalent of (flow.md §7.6).
start controller_in   mocks.gate_controller --gate 1
start controller_out  mocks.gate_controller --gate 2 --exit-lane
sleep 1

start api trafix.server
sleep 3

start orchestrator trafix.orchestrator
sleep 1

echo
echo "ready. In another terminal:"
if [[ -x ".venv/bin/trafix" ]]; then
  echo "  source .venv/bin/activate"
  echo "  trafix status"
  echo "  trafix enter --plate 'H 488 AI'         # a vehicle checks in"
  echo "  trafix txn list"
  echo "  trafix-cashier lookup --ticket <code>"
  echo "  trafix-cashier settle --ticket <code> -y"
else
  echo "  $PYTHON -m cli.trafix status"
  echo "  $PYTHON -m cli.trafix enter --plate 'H 488 AI'    # a vehicle checks in"
  echo "  $PYTHON -m cli.trafix txn list"
  echo "  $PYTHON -m cli.cashier lookup --ticket <code>"
  echo "  $PYTHON -m cli.cashier settle --ticket <code> -y"
  echo
  echo "  (run '$PYTHON -m pip install -e .' for the shorter 'trafix ...' form)"
fi
echo
echo "following logs — Ctrl-C to stop everything"
echo
tail -f "$LOG_DIR"/orchestrator.log "$LOG_DIR"/api.log "$LOG_DIR"/controller_in.log
