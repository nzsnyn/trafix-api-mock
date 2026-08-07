#!/usr/bin/env bash
# On-site run against the LIVE broker/hardware, writing to a LOCAL staging
# copy of Parkways (nothing writes to the production database).
#
# Copy this file to the repo root on parkways (192.168.1.1) and run:
#   ./onsite_staging.sh
#
# Flags:
#   --skip-db-setup   keep the existing staging DB, just start the services
#
# Env overrides:
#   STAGING_DB_URL    where the staging copy lives (default: local docker PG)
#   API_PORT          port for trafix-server (default 8000)
#   MQTT_USER/MQTT_PASS    real broker creds, if the config default is wrong
#   PGDUMP_USER/PGDUMP_PASS  live Parkways creds (default postgres/postgres)
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-.venv/bin/python}"
ENV_NAME=site
API_PORT="${API_PORT:-8000}"
BROKER_HOST="${BROKER_HOST:-192.168.1.1}"
STAGING_DB_URL="${STAGING_DB_URL:-postgresql+psycopg://trafix:trafix@127.0.0.1:5433/parkways_staging}"
PGDUMP_HOST="${PGDUMP_HOST:-192.168.1.1}"
PGDUMP_PORT="${PGDUMP_PORT:-5432}"
PGDUMP_DB="${PGDUMP_DB:-Parkways}"
PGDUMP_USER="${PGDUMP_USER:-postgres}"
PGDUMP_PASS="${PGDUMP_PASS:-postgres}"

REBUILD=1
[[ "${1:-}" == "--skip-db-setup" ]] && REBUILD=0

die() { echo "error: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
up()  { timeout 3 bash -c "cat < /dev/null > /dev/tcp/$1/$2" 2>/dev/null; }

[[ -x "$PYTHON" ]] || die "no interpreter at $PYTHON — create one: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"

echo "== phase 0: preflight (read-only) =="

up "$BROKER_HOST" 1883 || die "broker not reachable at $BROKER_HOST:1883 — are you on the site LAN? (export BROKER_HOST if the broker is elsewhere, e.g. 127.0.0.1)"
echo "broker $BROKER_HOST:1883  reachable"

if [[ -f scripts/observe.py ]]; then
  echo "checking broker auth (read-only observer, 3s)..."
  if ! timeout 20 "$PYTHON" -m scripts.observe --env site --timeout 3 >/dev/null 2>&1; then
    echo "warning: broker auth failed with default creds (bssparking/BCTDev_2025)."
    echo "         export MQTT_USER/MQTT_PASS from mqtt_app_lpr/.env and rerun."
    exit 1
  fi
  echo "broker auth OK"
else
  echo "note: scripts/observe.py missing here — skipping auth check (creds are still used at connect)"
fi

have psql || die "psql not installed (apt install postgresql-client)"
have pg_dump || die "pg_dump not installed (apt install postgresql-client)"
if ! PGPASSWORD="$PGDUMP_PASS" psql -h "$PGDUMP_HOST" -p "$PGDUMP_PORT" -U "$PGDUMP_USER" -d "$PGDUMP_DB" -tAc 'select 1' >/dev/null 2>&1; then
  echo "warning: cannot read live Parkways with $PGDUMP_USER / (password from PGDUMP_PASS)."
  echo "         export PGDUMP_USER/PGDUMP_PASS from mqtt_app_lpr/.env and rerun."
  exit 1
fi
echo "live DB $PGDUMP_HOST:$PGDUMP_PORT/$PGDUMP_DB  reachable"

q() { PGPASSWORD="$PGDUMP_PASS" psql -h "$PGDUMP_HOST" -p "$PGDUMP_PORT" -U "$PGDUMP_USER" -d "$PGDUMP_DB" -tAc "$1"; }
for col in members.card_number transactions.paid_at transactions.keterangan transactions.camout_lpr; do
  t="${col%%.*}"; c="${col##*.}"
  n="$(q "select count(*) from information_schema.columns where table_name='$t' and column_name='$c'")"
  echo "  schema  $col  ->  $([ "$n" = 1 ] && echo present || echo MISSING)"
done

if up 127.0.0.1 "$API_PORT"; then
  echo "warning: port $API_PORT already in use — set API_PORT=8001 (and keep it for the smoke test)"
  exit 1
fi
echo "port $API_PORT  free"

if (( REBUILD )); then
  echo
  echo "== phase 1: staging DB on local docker postgres =="
  have docker || die "docker not available"
  docker compose up -d postgres
  for _ in $(seq 1 30); do up 127.0.0.1 5433 && break; sleep 1; done
  up 127.0.0.1 5433 || die "local postgres did not come up on 127.0.0.1:5433"
  if [[ "$(docker compose exec -T postgres psql -U trafix -d parkways -tAc "select 1 from pg_database where datname='parkways_staging'")" != 1 ]]; then
    docker compose exec -T postgres psql -U trafix -d parkways -c "create database parkways_staging"
  fi
  echo "snapshotting live Parkways -> parkways_staging..."
  PGPASSWORD="$PGDUMP_PASS" pg_dump -h "$PGDUMP_HOST" -p "$PGDUMP_PORT" -U "$PGDUMP_USER" -d "$PGDUMP_DB" \
    | docker compose exec -T postgres psql -U trafix -d parkways_staging >/dev/null
  echo "applying additive schema fixes (staging only)..."
  docker compose exec -T postgres psql -U trafix -d parkways_staging \
    -c "ALTER TABLE members ADD COLUMN IF NOT EXISTS card_number VARCHAR(255) NULL;" \
    -c "CREATE INDEX IF NOT EXISTS idx_members_card_number ON members(card_number);"
  echo "staging DB ready: $STAGING_DB_URL"
fi

echo
echo "== phase 2+3: starting API server and orchestrator =="
mkdir -p logs
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
  "$PYTHON" -m "$@" >"logs/$name.log" 2>&1 &
  PIDS+=("$!")
  echo "  $name (pid ${PIDS[-1]}) -> logs/$name.log"
}

export TRAFIX_DB_SITE="$STAGING_DB_URL"
export TRAFIX_API_URL="http://127.0.0.1:$API_PORT"

start api trafix.server --env "$ENV_NAME" --port "$API_PORT"
sleep 4
up 127.0.0.1 "$API_PORT" || { echo "server did not start — see logs/api.log"; exit 1; }
echo "  api health: $("$PYTHON" -c "import urllib.request,sys;print(urllib.request.urlopen('http://127.0.0.1:$API_PORT/api/health',timeout=3).read().decode())" 2>&1)"

start orchestrator trafix.orchestrator --env "$ENV_NAME" --rfid-only --vehicle-id 1
sleep 2

echo
echo "ready. On this box (venv active):"
echo "  TRAFIX_API_URL=http://127.0.0.1:$API_PORT .venv/bin/trafix-cashier --env site lookup --ticket <code>"
echo "  TRAFIX_DB_SITE=$STAGING_DB_URL .venv/bin/trafix --env site txn list --limit 5"
echo
echo "Entry: tap the member RFID card at the real gate -> real .204 opens."
echo "Exit:  real .149 plate reads -> automated gateout against staging (notfound for live cars)."
echo
echo "following logs — Ctrl-C to stop everything"
tail -f logs/orchestrator.log logs/api.log
