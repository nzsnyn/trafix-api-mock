# Trafix — Python implementation of the LPR parking system

A Python rebuild of the parking system at **Sinode GKJ, Salatiga**, following
the protocol documented in [`flow.md`](flow.md) — which was reconstructed from
a 1 GB packet capture of the live site and cross-checked against the Laravel
source in `../Trafix/mqtt_app_lpr`.

It ships the whole site: the API server, the MQTT orchestrator, mock gate
hardware, and a cashier client. The entire check-in / check-out cycle runs on
one laptop with no hardware attached.

```
                     MQTT (mosquitto :1883)
   gate controller .204 ─────┐        ┌───── exit controller (does not exist on site)
   relay + thermal printer   │        │
                       ┌─────┴────────┴─────┐
                       │  ORCHESTRATOR  .1  │  ← the component missing from production
                       └─────────┬──────────┘
                                 │ HTTP (loopback)
                       ┌─────────┴──────────┐
                       │   API SERVER  .1   │──── PostgreSQL "Parkways"
                       └─────────┬──────────┘
              HTTP :8090         │         MQTT gate/out/1/pos
   entry LPR .130 ───────────────┘─────────────── exit LPR .149
                                 │
                        cashier desk .2
```

---

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # installs the project + the commands
source .venv/bin/activate

docker compose up -d mosquitto postgres
./run_sim.sh --fresh
```

In another terminal (with the venv activated):

```bash
trafix status
trafix enter --plate 'H 488 AI'      # a vehicle takes a ticket and drives in
trafix txn list

trafix-cashier lookup --ticket <code>     # what does it owe?
trafix-cashier settle --ticket <code> -y  # take payment, open the exit
```

`trafix tail` shows every MQTT message as it happens.

## The commands

`pip install -e .` puts six commands on PATH. They work from any directory.

| Command | What it is |
|---|---|
| `trafix` | drive and inspect the simulated site |
| `trafix-cashier` | the exit desk — stands in for the Tauri app at `.2` |
| `trafix-server` | the API server (the Laravel app's replacement) |
| `trafix-orchestrator` | the MQTT component missing from production |
| `trafix-lpr` | a mock LPR camera unit |
| `trafix-controller` | a mock gate controller board |

Every one accepts `--env` (`sim`, `e2e`, `site`). Without installing, the
equivalent `python -m cli.trafix`, `python -m trafix.server`, `python -m
mocks.lpr_unit` … forms all still work.

### Driving the site

```bash
trafix status                          # is every component reachable?
trafix enter --plate 'H 488 AI'        # the whole entry sequence
trafix arrive / trafix press / trafix pass    # or step it manually
trafix exit-read --plate 'H 488 AI'    # exit camera announces a plate
trafix txn list [--inside]             # the ledger
trafix txn show <ticket|plate>         # one session and its event trail
trafix tail                            # live MQTT traffic
```

Fault injection, to see how the system degrades:

```bash
trafix lpr --gate 1 --mode error       # camera returns 503
trafix lpr --gate 1 --mode no_plate    # camera reads nothing
trafix lpr --gate 1 --queue 'B 1234 XYZ'   # force the next read
trafix lpr --gate 1 --mode ok          # back to normal
```

### The cashier desk

```bash
trafix-cashier lookup --ticket <code>       # price it, read-only
trafix-cashier settle --ticket <code> [-y]  # take payment, release the vehicle
trafix-cashier lost --plate 'H 488 AI'      # lost ticket, flat penalty
```

**What needs what:** `status` and `txn` need only PostgreSQL. `enter`, `press`,
`exit-read` and `lpr` need the broker plus the mocks and orchestrator.
`trafix-cashier` needs the API server.

### Finding the configuration

`devices.yaml` is looked up in this order, so an installed command works from
anywhere and a site deployment can keep its config outside the source tree:

1. `$TRAFIX_CONFIG` — full path to a `devices.yaml`
2. `./config/devices.yaml` relative to the current directory
3. the `config/` directory in this source tree

`$TRAFIX_STORAGE` overrides where snapshots are written; otherwise a relative
`storage_dir` resolves next to whichever `config/` was used.

---

## What this fixes

`flow.md` documents a system whose automated LPR gate-out is completely dead.
This implementation fixes each defect and says so in the code.

| flow.md | Defect on site | Here |
|---|---|---|
| **§7.1** | `POST /api/lpr/gateout` → **500**. `routes/api.php:205` points at `GateoutController::GateOutLpr`, which was never written. One 500 per exit event. | Implemented in `trafix/service.py::gate_out`, modelled on `GateOutRfidLpr:1603`. |
| **§7.6** | **Nothing commands the exit barrier.** `GateoutController` contains no MQTT publish at all. How the barrier physically opens is undetermined. | The API publishes `outputCtrl` to `/GATE/OUT/{gate}` on a successful exit. Disable with `command_exit_barrier: false`. |
| **§7.7** | Plate lookup matches `police_number` exactly, but the two cameras disagree (`H488AI` vs `H4818AI`) and **4 of 6 entry tickets recorded no plate at all**. | The ticket code is authoritative, the plate advisory. A mismatch is recorded in `keterangan`, not enforced — unless you set `require_plate_match: true`. |
| **§7.7** | `gate_status='in'` and `time_checkout IS NULL` are used inconsistently to mean "still inside". | `time_checkout IS NULL` is authoritative — it is written exactly once and predates `gate_status` by two years. |
| **§7.5** | CCTV snapshot 404s on all 21 attempts: Dahua's `snapshot.cgi` path requested from Uniview cameras, no auth header. | Path is configurable per camera and digest auth is sent when credentials are set. **Confirm the real path against the camera firmware.** |
| **§2** | Image downloads are queued jobs; with no `queue:work` running they silently never happen. | Downloads run on a thread pool inside the API process. No worker to forget. |
| **§2** | The orchestrator that opens the barrier **is not in the repo at all**. | `trafix/orchestrator.py` — a clean-room reconstruction from the §5 message sequence. |

Two things are reproduced faithfully rather than fixed, because changing them
would change what customers are charged or what the printer receives:

- **`uartDataLen` counts hex characters, not bytes.** The PHP does `strlen()`
  on the hex string. The hardware accepts it; halving it would be a silent
  protocol change.
- **Multi-day stays are undercharged.** `CalculateRate` reads the *hour
  component* of the interval, so 26 hours bills as 2. `calculate(...,
  fix_multiday=True)` charges the true elapsed hours — turn it on only once
  the operator agrees the prices should change.

---

## The wire protocol

Every constant below was observed on the wire. Changing one stops real
hardware understanding us.

### MQTT

| Topic | Direction | Purpose |
|---|---|---|
| `/GATE/event/{gate}` | controller → server | `inputInfo` sensor state, command acks |
| `/GATE/IN/{gate}` | server → controller | `txUartData` (print), `outputCtrl` (relay) |
| `/GATE/IN/{gate}/status` | server → LPR | `{"status":"welcome"}` / `"thanks"` |
| `gate/out/{gate}/pos` | exit LPR → server | `{"plate_num":…,"url_gambar":…}` |
| `/GATE/OUT/{gate}` | server → exit controller | **added here** — production has no equivalent (§7.6) |

Note the exit LPR's lower-case convention. That is how the device is
configured on site; it is reproduced, not tidied up.

Envelope:

```json
{"id":"<md5>","serialNo":"441D6491AF17","version":"1.0","taskNo":2,
 "method":"txUartData|outputCtrl|inputInfo|status","data":{}}
```

Barrier open — the only relay command seen in 43 minutes of capture:

```json
{"method":"outputCtrl","serialNo":"441D6491AF17","taskNo":1,"version":"1.0",
 "data":{"beepOut":[1,100],"relay1Out":[1,1000]}}
```

Sensor map (⚠️ **inferred** from state transitions, not a datasheet):
`input3` arrival loop · `input2` ticket button · `input4` pass-through ·
`input1` never seen active. `relay1` is the barrier.

### Ticket printing

`txUartData` payloads are **doubly encoded**: JSON → `data[].uartData` is a hex
string → the hex decodes to raw ESC/POS bytes. The two halves are published
**200 ms apart**; the controller drops the second if they arrive together.

`trafix/escpos.py` is a byte-for-byte port of `TicketPrinterService.php`, and
`tests/test_escpos.py` asserts its output against the real ticket decoded off
the wire:

```
Sinode GKJ
Jl. Dr. Sumardi No. 8, Kec.
Sidorejo, Kota Salatiga, Jawa
Tengah
[QR] 9922432407
Simpan QR untuk Keluar Parkir
Plat: H488AI
9922432407-Motor-IN 1
Enter Time: 2026-07-24 03:52:02
--------------------------
Tiket hilang motor Rp10000 mobil Rp30000
Denda inap motor Rp10000 mobil Rp25000
```

### HTTP

| Endpoint | Method | Production | Here |
|---|---|---|---|
| `/api/gatein` | POST | ✅ (over loopback) | ✅ |
| `/api/gatein/card` | POST | ✅ | ✅ |
| `/api/lpr/gatein` | POST | ✅ | ✅ |
| `/api/lpr/gateinimage` | POST | ✅ | ✅ |
| `/api/lpr/checkimage` | POST | ✅ | ✅ |
| `/api/gateout/detailtransaction` | POST | ✅ 200 | ✅ |
| `/api/gateout/gateoutKasir` | PUT | ✅ 200 | ✅ + opens the barrier |
| `/api/lpr/gateout` | POST | ❌ **500** | ✅ **implemented** |
| `/api/lpr/gateoutcard` | PUT | ✅ | ✅ |
| `/api/lpr/checkimagegateout` | POST | ❌ 404 | ✅ |
| `.130:8090/checklpr` | GET | ✅ 200 | mocked |

The cashier app sends `Origin: tauri://localhost`, so CORS preflight handling
is load-bearing.

---

## The entry sequence

Reconstructed from flow.md §5 and implemented in `trafix/orchestrator.py`:

1. `inputInfo input3=1` — vehicle on the arrival loop
2. server → `/GATE/IN/1/status` `welcome`
3. `inputInfo input2=1` — ticket button pressed
4. server → `GET .130:8090/checklpr` — read the plate
5. server → `POST /api/gatein` — ticket issued
6. server → `txUartData` ×2, 200 ms apart — ticket prints
7. server → `/GATE/IN/1/status` `thanks`
8. server → `outputCtrl relay1` — **barrier opens**
9. `inputInfo input4=1` — vehicle clears the lane

Arrival to barrier is about 4.4 s on site; the full cycle about 10 s.

---

## Configuration

`config/devices.yaml`, selected with `TRAFIX_ENV`:

| Env | What |
|---|---|
| `sim` (default) | everything on localhost, LPRs on `:8090` / `:8091` |
| `e2e` | isolated ports and its own database, used by the test suite |
| `site` | the real `192.168.1.x` addresses |

The `site` block reproduces two known device faults so you can see how the
system degrades: `lpr_out.serves_http: false` (the real `.149:8090` is POST-only
and rejects GET — the advertised image URL still can't be fetched) and the
corrected Uniview snapshot path.

⚠️ **Before deploying to site**, settle the open questions in flow.md §11 —
above all: where is the existing orchestrator, and does it need stopping before
this one starts? Two publishers sharing a fixed MQTT client ID evict each other.

---

## Tests

```bash
.venv/bin/python -m pytest              # 113 tests, no Docker needed
docker compose up -d mosquitto postgres
.venv/bin/python -m pytest tests/test_e2e.py    # the whole site, real processes
```

All 124 pass with both services up.

| File | Covers |
|---|---|
| `test_escpos.py` | ticket bytes vs. the real captured ticket |
| `test_protocol.py` | topics, envelope, barrier command |
| `test_rates.py` | `CalculateRate`, including the pinned PHP defects |
| `test_service.py` | check-in / check-out against a database |
| `test_api.py` | HTTP surface and response shapes |
| `test_e2e.py` | every process, over MQTT and HTTP |

---

## Layout

```
trafix/
  protocol.py      MQTT topics, envelope, command builders
  escpos.py        ticket construction (port of TicketPrinterService)
  rates.py         fee calculation (port of CalculateRate)
  models.py        the Parkways schema, mirrored from the Laravel migrations
  db.py            engine, sessions, site seed data
  service.py       check-in / check-out logic, incl. the missing GateOutLpr
  api.py           HTTP API (the Laravel app's replacement)
  orchestrator.py  the MQTT component missing from production
  publisher.py     gate commands over MQTT
  storage.py       snapshot fetching
  mqtt_bus.py      the only module importing paho
  server.py        API entrypoint
mocks/
  gate_controller.py   the .204 relay + printer board
  lpr_unit.py          the .130 and .149 camera units
cli/
  trafix.py        drive and inspect the simulator
  cashier.py       stands in for the Tauri app at .2
config/devices.yaml
pyproject.toml     packaging + the six console commands
greenfield_prototype/  an earlier clean-slate design, kept for reference only
```

`cli` and `mocks` are top-level package names. Inside this project's own venv
that is harmless. If this is ever published to a package index, move them under
`trafix/` (`trafix.cli`, `trafix.mocks`) to avoid claiming generic names.
