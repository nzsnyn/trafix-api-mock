# Hardware Requirements

Physical devices needed to run the Trafix parking system on a real site.

---

## Device Inventory

| # | Device | Model/Type | IP | Purpose | Required |
|---|--------|-----------|-----|---------|----------|
| 1 | Server | Laptop or PC | `192.168.1.1` | Runs API + orchestrator + MQTT broker | Yes |
| 2 | MQTT Broker | Mosquitto | `192.168.1.1:1883` | Message bus between all components | Yes |
| 3 | PostgreSQL | PostgreSQL 16 | `192.168.1.1:5432` | Transaction database | Yes |
| 4 | Entry LPR Camera | LPR unit with HTTP API | `192.168.1.130:8090` | Read plates on entry | Yes |
| 5 | Exit LPR Camera | LPR unit with MQTT | `192.168.1.149:8090` | Read plates on exit | Yes |
| 6 | Entry Gate Controller | Relay board + thermal printer | `192.168.1.204` | Entry barrier + ticket printing | Yes |
| 7 | Exit Gate Controller | Relay board | TBD | Exit barrier control | Yes |
| 8 | Entry CCTV | Uniview IP camera | `192.168.1.148` | Entry snapshot capture | Optional |
| 9 | Exit CCTV | Uniview IP camera | `192.168.1.150` | Exit snapshot capture | Optional |
| 10 | Receipt printer | Network ESC/POS | `192.168.1.168` | Ticket receipt printing (referenced in `config/receiptprinter.php`) | Optional |

---

## Network Requirements

- **Subnet:** `192.168.1.0/24`
- **MQTT broker:** Port `1883` (TCP), accessible from all devices
- **LPR cameras:** Port `8090` (HTTP), server must reach them
- **Gate controller:** MQTT only, no HTTP needed
- **Server API:** Port `8000`, reachable from cashier app (`.2`)
- **PostgreSQL:** Port `5432`, server connects locally

---

## Device Protocols

### Entry LPR Camera (`.130`)

| Protocol | Direction | Endpoint/Topic | Purpose |
|----------|-----------|----------------|---------|
| HTTP GET | Server → LPR | `/checklpr` | Read the plate |
| HTTP GET | Server → LPR | `/image/{id}.jpg` | Fetch snapshot |
| MQTT subscribe | LPR ← Server | `/GATE/IN/1/status` | Display signage (welcome/thanks) |

**Response format:**
```json
{"plate_num": "H488AI", "url_gambar": "http://192.168.1.130:8090/image/H488AI.jpg"}
```

### Exit LPR Camera (`.149`)

| Protocol | Direction | Endpoint/Topic | Purpose |
|----------|-----------|----------------|---------|
| MQTT publish | LPR → Server | `gate/out/1/pos` | Announce plate read |

**⚠️ Image downloads from exit still fail.** On 2026-08-03 `.149` answered on `:8090`
(`pw-signage-gateout-server`) but rejects GET with `405 Only POST method is allowed`; SSH `:22`,
Boa HTTPd `:80` and VNC `:5900` are also open. Only MQTT (publish) is used for the plate read.

**Published message:**
```json
{"plate_num": "B1234CD", "url_gambar": "http://192.168.1.149:8090/image/B1234CD.jpg"}
```

### Entry Gate Controller (`.204`)

| Protocol | Direction | Topic | Purpose |
|----------|-----------|-------|---------|
| MQTT publish | Controller → Server | `/GATE/event/1` | Sensor states + acks |
| MQTT subscribe | Controller ← Server | `/GATE/IN/1` | Print commands + barrier control |

**Published messages:**
```json
{"method":"inputInfo", "serialNo":"441D6491AF17", "data":{"input3":1, "input2":0, "input4":0, "input1":0}}
```

**Subscribed commands:**
- `txUartData` — ESC/POS ticket data for thermal printer
- `outputCtrl` — Relay/barrier control

### Exit Gate Controller (TBD)

| Protocol | Direction | Topic | Purpose |
|----------|-----------|-------|---------|
| MQTT publish | Controller → Server | `/GATE/event/2` | Sensor states + acks |
| MQTT subscribe | Controller ← Server | `/GATE/OUT/2` | Barrier control |

**⚠️ Not configured.** No exit gate controller is identified on site. See [Exit Gate Gap](#exit-gate-gap) below.

### Uniview Cameras (`.148`, `.150`)

| Protocol | Direction | Endpoint | Purpose |
|----------|-----------|----------|---------|
| HTTP GET | Server → Camera | `/images/snapshot.jpg` | Capture snapshot |

**Authentication:** Basic auth (user: `admin`, password via `CAM_PASS` env var)

**⚠️ Production used wrong path.** The original Laravel code uses Dahua's `/cgi-bin/snapshot.cgi` which 404s on Uniview cameras. This codebase corrects it to `/images/snapshot.jpg`.

---

## Sensor Map

From the entry gate controller (`.204`), inferred from packet capture:

| Input | Meaning | Notes |
|-------|---------|-------|
| `input3` | Arrival loop | Vehicle detected at gate |
| `input2` | Ticket button | Driver presses to get ticket |
| `input4` | Pass-through loop | Vehicle cleared the lane |
| `input1` | Unknown | Never observed active |

| Relay | Meaning | Notes |
|-------|---------|-------|
| `relay1` | Entry barrier | Pulses 1000ms to open |
| `relay2` | Unknown | Never actuated |
| `relay3` | Unknown | Never actuated |

---

## Configuration Status

| Item | Status | Action Needed |
|------|--------|---------------|
| Entry LPR `.130` | Configured | Verify HTTP `/checklpr` works |
| Exit LPR `.149` | Configured; `:8090` POST-only (GET 405) | Confirm the intended POST API / image path |
| Entry gate `.204` | Configured | Verify serial `441D6491AF17` |
| Exit gate | **Not configured** | Find IP and serial number |
| Entry camera `.148` | Configured | Verify snapshot path works |
| Exit camera `.150` | Configured | Verify snapshot path works |
| MQTT broker | Configured | Set `MQTT_PASS` env var |
| PostgreSQL | User configured | Verify schema compatibility |

---

## Exit Gate Gap

The production site has no identified exit gate controller. Options:

1. **Same board as entry** — if the `.204` board has 2 relay channels, one for entry and one for exit. Check with hardware vendor.

2. **Separate board** — a second relay board at a different IP. Need to discover its IP and serial number. Check the MQTT broker for any other `serialNo` values.

3. **Manual operation** — cashier opens exit barrier manually (current production behavior). The `command_exit_barrier: true` policy in `config/devices.yaml` enables software control, but requires a configured exit controller.

**To discover the exit controller:**
```bash
# Watch all MQTT traffic for any device serial numbers
trafix --env site tail
```

Or check the Mosquitto logs for any connections from unknown IPs.

---

## Shopping List

For a new 1-entry/1-exit parking site:

| Item | Qty | Notes |
|------|-----|-------|
| LPR camera (with HTTP + MQTT) | 2 | Entry and exit units |
| Gate controller (relay + printer) | 1-2 | Entry needs printer, exit needs relay only |
| Thermal printer | 1 | Built into gate controller or separate |
| Barrier gate with induction loop | 1-2 | Entry and exit |
| MQTT broker (Raspberry Pi or server) | 1 | Mosquitto in Docker |
| Server (laptop or mini PC) | 1 | Runs API + orchestrator |
| Network switch | 1 | 8-port minimum |
| Ethernet cables | 7+ | One per device |
| IP cameras (optional) | 2 | Uniview or compatible |

---

## Deployment Checklist

### 1. Infrastructure Setup

```bash
# Start Docker services
docker compose up -d

# Initialize database
./run_sim.sh --fresh
```

### 2. Verify Simulation

```bash
# Run full simulation
./run_sim.sh

# In another terminal
trafix status
trafix enter --plate 'H 488 AI'
trafix txn list
```

### 3. Configure Site Environment

Set environment variables:
```bash
export TRAFIX_ENV=site
export MQTT_PASS=<bssparking_password>
export CAM_PASS=<camera_password>
```

Edit `config/devices.yaml` if needed:
- Update `gate_controller_out` with real exit controller IP/serial
- Verify all IPs match your network
- Adjust camera snapshot paths if needed

### 4. Deploy to Server

```bash
# On the server (192.168.1.1)
TRAFIX_ENV=site MQTT_PASS=<password> python -m trafix.server &
TRAFIX_ENV=site MQTT_PASS=<password> python -m trafix.orchestrator &
```

---

## On-Site Testing Guide

### 1. Pre-Flight Checks

Before connecting any real devices, verify the server infrastructure is healthy.

```bash
# Start server infrastructure
docker compose up -d mosquitto postgres

# Initialize database (seeds Sinode GKJ data)
./run_sim.sh --fresh

# Verify services
trafix status
```

**Expected output:**
```
environment : sim
database    : postgresql+psycopg://***@127.0.0.1:5433/parkways
broker      : 127.0.0.1:1883  [reachable]
api         : http://127.0.0.1:8000  [ok]
lpr_in      : http://127.0.0.1:8090  [ok]
lpr_out     : http://127.0.0.1:8091  [ok]
gate_controller_in  : gate 1, serial 441D6491AF17
gate_controller_out : gate 2, serial 441D6491AF18

transactions: 0 total, 0 still inside
```

Set environment variables for the real site:
```bash
export TRAFIX_ENV=site
export MQTT_PASS=<password>
export CAM_PASS=<password>
```

```bash
# Kill simulation processes before testing real devices
pkill -f "trafix-lpr|trafix-controller|trafix-server|trafix-orchestrator"
```

### 2. Device Discovery

On-site devices need to be found and verified before the system can talk to them.

```bash
# Scan the network for all devices
nmap -sV --open 192.168.1.0/24 -p 1883,5432,8090,8000,80
```

**What to check for each device:**

| Device | Check | Command |
|--------|-------|---------|
| MQTT broker | Port 1883 open | `nmap -p 1883 192.168.1.1` |
| PostgreSQL | Port 5432 open | `nmap -p 5432 192.168.1.1` |
| Entry LPR | Port 8090 + HTTP responds | `curl -v http://192.168.1.130:8090/checklpr` |
| Exit LPR | Port 8090 accepts POST only | `curl -v -X POST http://192.168.1.149:8090/` |
| Gate controller | MQTT publishes events | (see test below) |
| Cameras | HTTP responds | `curl -v http://192.168.1.148/images/snapshot.jpg` |

**Verify device serial numbers:**

The entry gate controller should have serial `441D6491AF17` (from `devices.yaml`). To confirm:
```bash
# Subscribe to all MQTT events and look for serialNo values
trafix --env site tail
```
If the real device has a different serial number, update `config/devices.yaml`.

### 3. Individual Device Tests

Test each component in isolation before attempting a full flow.

#### 3a. MQTT Broker

```bash
# Subscribe to all topics (should show existing traffic from real devices)
mosquitto_sub -h 192.168.1.1 -p 1883 -u bssparking -P "$MQTT_PASS" -t "#" -v

# Publish a test message
mosquitto_pub -h 192.168.1.1 -p 1883 -u bssparking -P "$MQTT_PASS" \
  -t "/GATE/event/test" -m '{"method":"ping"}'
```

**Pass criteria:** Broker accepts connection. Existing device messages appear within 30 seconds.

#### 3b. PostgreSQL

```bash
# Check the database has the correct schema
psql -h 192.168.1.1 -U trafix -d parkways -c "\dt"

# Expected tables:
# admins, gate_events, locations, members, parking_fees, transactions, vehicles, xendit_qr_pool

# Check seed data exists
psql -h 192.168.1.1 -U trafix -d parkways -c "SELECT name, address FROM locations;"
```

**Pass criteria:** All tables exist. `Sinode GKJ` location is present.

#### 3c. Entry LPR Camera (`.130`)

```bash
# Test HTTP endpoint — asks the camera for its current plate read
curl -v http://192.168.1.130:8090/checklpr 2>&1

# Expected JSON response:
# {"plate_num":"H488AI","url_gambar":"http://192.168.1.130:8090/image/H488AI.jpg"}

# Test image serving
curl -v http://192.168.1.130:8090/image/H488AI.jpg -o /tmp/test_plate.jpg
file /tmp/test_plate.jpg
```

**Pass criteria:** Returns valid JSON with `plate_num` and `url_gambar`. Image downloads as a JPEG.

**Failure modes:**
- `Connection refused` — camera is not listening on port 8090
- `Empty plate_num` — no vehicle at camera, or camera needs reset
- `404` — wrong URL path

#### 3d. Exit LPR Camera (`.149`)

```bash
# Subscribe to the topic the exit LPR publishes on
mosquitto_sub -h 192.168.1.1 -p 1883 -u bssparking -P "$MQTT_PASS" \
  -t "gate/out/1/pos" -v
```

Have a vehicle drive past the exit camera (or trigger a manual plate read on the device).

**Pass criteria:** A message appears within 30 seconds:
```json
{"plate_num":"B1234CD","url_gambar":"http://192.168.1.149:8090/image/B1234CD.jpg"}
```

**Note:** The `url_gambar` from the exit LPR is still not downloadable — `.149:8090` returns 405 to GET (POST-only signage server). This is expected — the plate number is what matters.

**Failure modes:**
- `No messages in 60 seconds` — exit LPR is not publishing, or wrong MQTT auth
- `Incorrect topic` — try `gate/out/#` or check device configuration

#### 3e. Entry Gate Controller (`.204`)

```bash
# Subscribe to events from the gate controller
mosquitto_sub -h 192.168.1.1 -p 1883 -u bssparking -P "$MQTT_PASS" \
  -t "/GATE/event/1" -v
```

Physically interact with the gate:
- Drive a vehicle onto the entry loop
- Press the ticket button
- Drive through the pass-through loop

**Pass criteria:** Sensor events appear in real-time:
```json
{"method":"inputInfo","serialNo":"441D6491AF17","data":{"input3":1,"input2":0,"input4":0,"input1":0}}
```

When the barrier command is sent later, the controller should also `ack` — with
**empty data** (verified on site 2026-08-04):
```json
{"method":"outputCtrl","serialNo":"441D6491AF17","data":{}}
```

The controller also publishes a cumulative `status` heartbeat (roughly every 50 s):
```json
{"method":"status","serialNo":"441D6491AF17","data":{"input1":0,"input2":0,"input3":0,"input4":0,"relay1":0,"relay2":0,"relay3":0,"beep":0}}
```

**Failure modes:**
- `No messages` — controller not connected to MQTT, or serial number mismatch
- `Wrong `serialNo`` — update `config/devices.yaml` with the real serial

#### 3f. Exit Gate Controller (TBD — if exists)

```bash
# Subscribe to exit events
mosquitto_sub -h 192.168.1.1 -p 1883 -u bssparking -P "$MQTT_PASS" \
  -t "/GATE/event/2" -v
```

#### 3g. CCTV Cameras (`.148`, `.150`)

```bash
# Test snapshot endpoint
curl -v -u "admin:$CAM_PASS" http://192.168.1.148/images/snapshot.jpg -o /tmp/snap_in.jpg
file /tmp/snap_in.jpg

curl -v -u "admin:$CAM_PASS" http://192.168.1.150/images/snapshot.jpg -o /tmp/snap_out.jpg
file /tmp/snap_out.jpg
```

**Pass criteria:** Returns a JPEG image (not 404 or empty).

**If the snapshot path is wrong:**
```bash
# Try common paths
curl http://192.168.1.148/cgi-bin/snapshot.cgi      # Dahua
curl http://192.168.1.148/onvif/snapshot             # ONVIF
curl http://192.168.1.148/axis-cgi/jpg/image.cgi     # Axis
```
Update the correct path in `config/devices.yaml` under `cameras`.

### 4. System Integration Tests

After every individual device passes, start the system and test end-to-end.

#### 4a. Start Services

```bash
# Terminal 1 — API server
TRAFIX_ENV=site MQTT_PASS=<password> CAM_PASS=<password> python -m trafix.server

# Terminal 2 — MQTT orchestrator (the brain)
TRAFIX_ENV=site MQTT_PASS=<password> python -m trafix.orchestrator

# Terminal 3 — MQTT traffic monitor
trafix --env site tail
```

```bash
# Verify everything is connected
trafix --env site status
```

**Expected:**
```
environment : site
database    : postgresql+psycopg://***@192.168.1.1:5432/Parkways
broker      : 192.168.1.1:1883  [reachable]
api         : http://192.168.1.1:8000  [ok]
lpr_in      : http://192.168.1.130:8090  [ok]
lpr_out     : http://192.168.1.149:8090  [no HTTP by design — reproduces §7.2]
gate_controller_in  : gate 1, serial 441D6491AF17
gate_controller_out : gate 2, serial (none)

transactions: <N> total, <M> still inside
```

#### 4b. Entry Flow Test

Follow the complete check-in sequence from `flow.md §5`:

| # | Action | Expected MQTT / HTTP | Check |
|---|--------|---------------------|-------|
| 1 | Driver arrives on entry loop | `inputInfo input3=1` on `/GATE/event/1` | Tail shows it |
| 2 | LPR display shows "welcome" | `/GATE/IN/1/status` `{"status":"welcome"}` | Observe on LPR screen |
| 3 | Driver presses ticket button | `inputInfo input2=1` on `/GATE/event/1` | Tail shows it |
| 4 | Server polls LPR for plate | `GET /checklpr` → `{"plate_num":"..."}` | Orchestrator log |
| 5 | Server issues ticket | `POST /api/gatein` → ticket code | Orchestrator log |
| 6 | Thermal printer prints ticket | `txUartData` ×2 on `/GATE/IN/1` (200ms apart) | Physical ticket ejected |
| 7 | LPR display shows "thanks" | `/GATE/IN/1/status` `{"status":"thanks"}` | Observe on LPR screen |
| 8 | Barrier opens | `outputCtrl relay1+beep` on `/GATE/IN/1` | Physical barrier rises |
| 9 | Driver clears lane | `inputInfo input4=1` on `/GATE/event/1` | Barrier lowers |

**Pass criteria:** Physical ticket printed with correct site info, QR code, and plate. Barrier opens. Transaction appears in database.

```bash
# Check the transaction was created
trafix --env site txn list
```

#### 4c. Cashier Exit Flow

| # | Action | Expected | Check |
|---|--------|----------|-------|
| 1 | Cashier scans ticket QR | `POST /api/gateout/detailtransaction` | `trafix-cashier lookup` |
| 2 | System calculates fee | Returns duration + total | Display shows amount |
| 3 | Cashier takes payment | `PUT /api/gateout/gateoutKasir` | `trafix-cashier settle` |
| 4 | Barrier opens | `outputCtrl` on `/GATE/OUT/2` | Physical barrier rises |
| 5 | Transaction marked paid | `payment_status='lunas'` | `trafix txn show` |

```bash
# Cashier testing commands
trafix-cashier --env site lookup --ticket <code>
trafix-cashier --env site settle --ticket <code> -y
trafix-cashier --env site lost --plate 'H 488 AI' -y
```

**Pass criteria:** Settlement succeeds. Barrier commands appear on MQTT. Transaction shows `lunas`.

**Note:** If no exit gate controller is configured, the `outputCtrl` command will still be published but no physical barrier will respond. You can verify the MQTT message was sent in the tail.

#### 4d. Automated LPR Exit

This is the path that was broken in production (always 500).

| # | Action | Expected | Check |
|---|--------|----------|-------|
| 1 | Exit LPR reads plate | `gate/out/1/pos` on MQTT | Tail shows plate |
| 2 | Orchestrator resolves ticket | `POST /api/lpr/gateout` | Orchestrator log |
| 3 | System settles automatically | If fee is 0 (member), gate opens | Barrier rises |
| 4 | Cashier handles fee | If fee > 0, ticket held for cashier | `trafix-cashier lookup` |

```bash
# Trigger an exit LPR read (need a vehicle or manual trigger)
# Then watch the orchestrator log to see what it decides
tail -f logs/orchestrator.log
```

**Pass criteria:** The orchestrator receives the plate, calls `/api/lpr/gateout`, and gets a 200 response (not 500 as in production).

#### 4e. Database Verification

```bash
# List all transactions
trafix --env site txn list

# Show full details of a transaction
trafix --env site txn show <code>

# Show gate events (audit trail)
trafix --env site txn show <code> | grep -A 20 events
```

**Pass criteria:** Every gate event (sensor, print, barrier, payment) is logged in `gate_events`.

### 5. Full Verification Checklist

- [ ] Server infrastructure: Docker services running, database seeded
- [ ] MQTT broker: reachable, authenticated, real device traffic visible
- [ ] Entry LPR `.130`: `/checklpr` returns plates, serves images
- [ ] Exit LPR `.149`: publishes plates on `gate/out/1/pos`
- [ ] Entry gate `.204`: publishes `inputInfo` on `/GATE/event/1`
- [ ] Cameras `.148`/`.150`: serve JPEG snapshots at correct path
- [ ] Exit gate: identified and configured (or manual operation confirmed)
- [ ] `devices.yaml` updated with correct IPs, ports, serial numbers
- [ ] Entry flow: ticket prints, barrier opens
- [ ] Cashier exit: settlement works, barrier commanded
- [ ] Automated exit: LPR plate read triggers check (even if auto-settle skipped)
- [ ] Gate events: every action logged in `gate_events` table
- [ ] Snapshots: images download to `storage/` directory
- [ ] `MQTT_PASS` and `CAM_PASS` env vars set (not hardcoded)

### 6. Troubleshooting

| Symptom | Likely Cause | Check |
|---------|--------------|-------|
| **Entry LPR unreachable** | Wrong IP/port | `curl -v http://192.168.1.130:8090/checklpr` |
| **Entry LPR returns empty plate** | No vehicle at camera, or camera needs reset | Verify vehicle is on the loop. Check camera logs. |
| **Exit LPR no plates on MQTT** | Not publishing, wrong topic, or auth failure | `mosquitto_sub -h .1 -t "gate/out/#" -v` |
| **Exit LPR HTTP dead** | Normal — `.149` doesn't serve HTTP | Nothing to fix. Only MQTT works. |
| **Gate controller silent** | Not connected to MQTT or wrong serial | Check device power/network. Verify `serialNo` in config. |
| **Barrier doesn't open** | No exit controller | Add `gate_controller_out` to `devices.yaml` (§ [Exit Gate Gap](#exit-gate-gap)) |
| **Ticket doesn't print** | Wrong serial or printer offline | Check printer paper. Verify `serialNo: 441D6491AF17` |
| **Ticket prints garbled** | Protocol mismatch | Verify ESC/POS bytes match `tests/test_escpos.py` |
| **Camera 404** | Wrong snapshot path | Try `/cgi-bin/snapshot.cgi` (Dahua) or `/images/snapshot.jpg` (Uniview) |
| **Camera auth failed** | Wrong credentials | Check `CAM_USER` / `CAM_PASS` env vars |
| **MQTT connection refused** | Wrong host/port or firewall | `nmap -p 1883 192.168.1.1` |
| **MQTT auth failed** | Wrong user/password | Verify `username: bssparking` in `devices.yaml`. Set `MQTT_PASS`. |
| **Orchestrator can't connect** | MQTT down or API down | Check both services. Run `trafix status`. |
| **API returns 500** | Database schema mismatch | Run seed. Check `gate_events` table exists. |
| **API returns 404** | Wrong endpoint | Check `devices.yaml` `api.base_url` |
| **Images not downloading** | Exit LPR HTTP dead, or camera unreachable | Normal for exit. For entry, check camera credentials. |
| **Xendit QR not working** | Location `status` not `"active"` | Set `status: active` in seed data. Refill QR pool. |
| **Cashier can't connect** | API not reachable from cashier desk | Server must bind `0.0.0.0`, not `127.0.0.1` (site config does this) |

### 7. Testing Without Physical Vehicles

When you don't have a vehicle to trigger sensors, you can:

**Simulate a vehicle (laptop on the same MQTT network):**

```bash
# Manually publish a sensor event as if the gate controller sent it
mosquitto_pub -h 192.168.1.1 -p 1883 -u bssparking -P "$MQTT_PASS" \
  -t "/GATE/event/1" \
  -m '{"method":"inputInfo","serialNo":"441D6491AF17","data":{"input3":1,"input2":0,"input4":0,"input1":0}}'
```

Wait for signage, then send the button press:
```bash
mosquitto_pub -h 192.168.1.1 -p 1883 -u bssparking -P "$MQTT_PASS" \
  -t "/GATE/event/1" \
  -m '{"method":"inputInfo","serialNo":"441D6491AF17","data":{"input3":1,"input2":1,"input4":0,"input1":0}}'
```

The orchestrator will react as if a real vehicle is present — it will poll the entry LPR and issue a ticket. The barrier command will be sent (no physical barrier without hardware) and a ticket transaction will appear in the database.

**Simulate an exit LPR plate read:**
```bash
mosquitto_pub -h 192.168.1.1 -p 1883 -u bssparking -P "$MQTT_PASS" \
  -t "gate/out/1/pos" \
  -m '{"plate_num":"H488AI","url_gambar":"http://192.168.1.149:8090/image/H488AI.jpg"}'
```

This is useful for testing the automated LPR exit path before you have a real vehicle at the exit gate.

---

## Reference

- `flow.md` — Full protocol documentation with evidence markers
- `config/devices.yaml` — All device addresses and policies
- `trafix/protocol.py` — MQTT wire format (reverse-engineered from capture)
