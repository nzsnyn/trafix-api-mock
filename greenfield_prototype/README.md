# Trafix — parking gate server and device simulator

The production parking server, plus mock LPR cameras and mock manless terminals
so the whole check-in / check-out flow runs on one laptop with no hardware.

```
                 MQTT (mosquitto :1883)
    manless_in ──────────┐        ┌────────── manless_out
      (.204)             │        │             (.2)
                      ┌──┴────────┴──┐
                      │   SERVER .1  │──── SQLite (trafix.db)
                      └──┬────────┬──┘
         HTTP REST       │        │      HTTP REST
    lpr_in (.130) ───────┘        └────────── lpr_out (.149)
```

The server is the real thing. The cameras and terminals are mocks that
implement the same contracts, so swapping in real hardware means editing
`config/devices.yaml` and nothing else.

---

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

docker compose up -d mosquitto     # the broker
./run_sim.sh                       # cameras + terminals + server
```

Then, in another terminal:

```bash
.venv/bin/python -m cli.trafix status              # is everything reachable?
.venv/bin/python -m cli.trafix press --lane in     # a driver takes a ticket
.venv/bin/python -m cli.trafix txn list            # see the ticket

# make the exit camera see the same car, then scan the ticket
.venv/bin/python -m cli.trafix camera --lane out --queue B1234XYZ
.venv/bin/python -m cli.trafix scan --lane out --barcode <barcode from above>
.venv/bin/python -m cli.trafix txn show <barcode>  # fee, duration, snapshots
```

`cli.trafix tail` shows every MQTT message as it happens.

---

## Configuration

Everything that knows an address lives in `config/devices.yaml`, selected with
`TRAFIX_ENV` (or `--env`):

| Environment | Where things run |
|---|---|
| `sim` (default) | all on `127.0.0.1`, cameras on `:8130` / `:8149` |
| `e2e` | same, on `:9130` / `:9149` with its own database and topic namespace — used by the test suite |
| `site` | the real device IPs |

> **The `site` subnet is a placeholder.** Only the last octets were known
> (server `.1`, manless_in `.204`, lpr_in `.130`, manless_out `.2`, lpr_out
> `.149`). Confirm the real prefix before deploying.

Tariff rules are in `config/tariff.yaml`; behaviour policies are under
`policies:` in `config/devices.yaml`.

---

## The contracts a real device must implement

### LPR camera — HTTP, called by the server

`POST /api/v1/capture`

```json
{ "trigger": "button", "lane": "in" }
```

```json
{
  "plate": "B1234XYZ",
  "confidence": 0.94,
  "image_url": "http://192.168.1.130/images/ab12cd34.jpg",
  "captured_at": "2026-07-26T13:37:04.461+00:00"
}
```

A camera that sees nothing returns `"plate": null` with a `message`. The
`image_url` must be fetchable over HTTP and return a JPEG.

`GET /api/v1/health` returns `{"status": "ok"}`.

The mock also exposes `/mock/queue`, `/mock/mode` and `/mock/reset` for driving
the simulation. A real camera does not need these.

### Manless terminal — MQTT

| Topic | Direction | Purpose |
|---|---|---|
| `trafix/{lane}/evt` | terminal → server | button press, ticket scan, gate feedback |
| `trafix/{lane}/cmd` | server → terminal | print ticket, open gate, checkout result |
| `trafix/{lane}/state` | terminal → broker | retained `online` / `offline`, set as the MQTT last will |
| `trafix/{lane}/sim` | CLI → mock terminal | simulator control only, not production |

`{lane}` is `in` or `out`. Every message uses one envelope:

```json
{
  "msg_id": "d3b0...",
  "correlation_id": "9f2a...",
  "ts": "2026-07-26T13:37:04.461+00:00",
  "device_id": "manless_in",
  "type": "ticket_request",
  "payload": {}
}
```

`correlation_id` on a command echoes the `msg_id` of the event that caused it.
That is what makes request/response work over pub/sub.

**Events** (terminal → server): `ticket_request`, `ticket_printed`,
`checkout_request`, `payment_settled`, `vehicle_passed`.

**Commands** (server → terminal): `print_ticket`, `checkout_result`,
`open_gate`, `reject`, `show_message`.

---

## The flows

**Check-in**

1. `ticket_request` — the driver presses the button
2. server calls `POST /api/v1/capture` on `lpr_in`
3. server writes the entry record and sends `print_ticket`
   (`ticket_no`, `barcode`, `plate`, `image_url`)
4. `ticket_printed` → server sends `open_gate`
5. `vehicle_passed` → transaction becomes `ACTIVE`

**Check-out**

1. `checkout_request` with the scanned `barcode`
2. server calls `POST /api/v1/capture` on `lpr_out`
3. server matches the exit plate against the entry plate and computes the fee
4. `checkout_result` (`plate_in`, `plate_out`, `plate_match`,
   `duration_minutes`, `fee`)
5. `payment_settled` → server sends `open_gate`
6. `vehicle_passed` → transaction becomes `CLOSED`

Transaction lifecycle: `PENDING → ACTIVE → AWAITING_PAYMENT → CLOSED`.

---

## What happens when things go wrong

These are configurable under `policies:` in `config/devices.yaml`.

| Situation | Default behaviour |
|---|---|
| Camera down, or plate unreadable at entry | `lpr_failure: allow` — issue the ticket with plate `UNKNOWN` and flag the record. A car is never trapped at the entry. Set to `deny` to refuse instead. |
| Exit plate ≠ entry plate | `plate_mismatch: flag` — the gate opens but the transaction is marked for operator review. Also `allow` or `deny`. |
| Exit plate unreadable | Not treated as a mismatch. A valid ticket still gets the driver out. |
| Entry plate was `UNKNOWN` | Not treated as a mismatch either. |
| Low-confidence read (below `lpr_min_confidence`) | Rejected — the doubted plate is recorded in the event log but never becomes the entry plate, so it cannot cause a phantom mismatch later. |
| Driver presses the button twice | `button_debounce_seconds: 5` — the same ticket is reprinted rather than a second one issued. |
| Unknown or already-used barcode | `checkout_result` carries an `error` and the gate stays shut. |
| Ticket whose car never completed entry | Refused (`not_active`). |
| Lost ticket | Resolved by matching the exit plate against active entries, charged the flat `lost_ticket` rate. |
| Terminal offline | Retained last-will on `trafix/{lane}/state`; the server logs it. |

---

## Tariff

`config/tariff.yaml`, applied in this order:

1. A lost ticket is a flat charge regardless of duration.
2. Inside `grace_minutes` the stay is free.
3. Otherwise `first_hour`, plus `next_hour` for every started hour after it.
4. Capped at `daily_max` per started 24 hours, then rounded up to `rounding`.

Defaults: 10 minutes free, IDR 3.000 first hour, IDR 2.000/hour after, IDR
25.000 daily cap, IDR 50.000 lost ticket, rounded to 500.

---

## Tests

```bash
.venv/bin/python -m pytest              # unit tests, no broker needed
docker compose up -d mosquitto
.venv/bin/python -m pytest tests/test_e2e.py    # full flow over the real wire
```

The end-to-end tests spawn every component as a real process and talk to them
over MQTT and HTTP. They skip themselves when no broker is running.

---

## Layout

```
trafix/            the production server
  config.py        device addressing, policies, tariff
  envelope.py      MQTT topics and the message envelope
  mqtt_bus.py      the only module that imports paho
  lpr_client.py    the only module that calls a camera
  db.py            the only module that touches SQLite
  tariff.py        fee calculation (pure)
  tickets.py       ticket numbers, barcodes, plate normalisation (pure)
  server/
    app.py         wiring and entrypoint
    checkin.py     entry state machine
    checkout.py    exit state machine
mocks/             fake devices
cli/trafix.py      drive and inspect the simulator
config/            devices.yaml, tariff.yaml
tests/
```
