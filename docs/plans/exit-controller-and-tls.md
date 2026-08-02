# Plan: Exit Gate Controller + TLS/HTTPS

Two known gaps from the project analysis:

1. **No exit gate controller on site** — the `site` env in `config/devices.yaml`
   has `gate_controller_out` commented out; nothing commands the exit barrier.
2. **No HTTPS/TLS** — all communication is plaintext: MQTT (barrier commands),
   HTTP API (transactions, payment data), and camera snapshot fetches
   (credentials).

---

## Plan 1: Configure Exit Gate Controller for Site

**Problem:** The `site` environment has `gate_controller_out` commented out in
`config/devices.yaml` (lines 131-134). The production site has no identified
exit gate controller — the exit barrier is never commanded by software
(`flow.md §7.6`).

The Python rewrite already supports the concept end-to-end:

- `trafix/publisher.py:50-59` publishes `outputCtrl` to `/GATE/OUT/{gate}` when
  `exit_lane=True`
- `trafix/service.py:610-613` calls `publisher.open_barrier(gate, exit_lane=True)`
  on settlement
- `trafix/protocol.py:46-56` defines `gate_out_topic()` — a new topic absent in
  production
- Mock controller supports `--exit-lane` flag subscribing to `/GATE/OUT/{gate}`
- The `command_exit_barrier: true` policy already exists

But on the real site, no device subscribes to `/GATE/OUT/{gate}`.

### Steps

**Phase 1 — Discovery (site survey):**

1. Run `trafix --env site tail` and watch for any unknown `serialNo` values
   appearing on MQTT — an exit controller might already be publishing events on
   `/GATE/event/2`
2. Check Mosquitto logs for MQTT connections from unknown IPs
3. Scan network: `nmap -sV --open 192.168.1.0/24 -p 1883` for MQTT-speaking
   devices
4. Physically inspect the exit barrier — is there a second relay board? Could
   the entry board (`.204`) have a second relay channel?

**Phase 2 — Configuration (once hardware is identified):**

1. Uncomment `gate_controller_out` block in `config/devices.yaml` under `site`
2. Fill in the discovered IP and serial number
3. Ensure `command_exit_barrier: true` is set in policies

**Phase 3 — Validation:**

1. Run `trafix --env site tail` and trigger a cashier settlement — verify
   `outputCtrl` appears on `/GATE/OUT/{gate}` and the physical barrier responds
2. Test the automated LPR exit path — the orchestrator's `_settle_by_plate`
   calls `POST /api/lpr/gateout` which triggers
   `publisher.open_barrier(gate, exit_lane=True)`

**Fallback if no hardware exists:**

- Keep `command_exit_barrier: false` (current effective behavior for site)
- Document as manual-only operation (the 3 options from `HARDWARE.md` Exit Gate
  Gap):
  - Option A: same board has 2 relays at `.204` — change topic to `/GATE/IN/1`
    with relay2
  - Option B: separate board at unknown IP — requires discovery
  - Option C: manual only — no code change, just documentation

### Files affected

| File | Change |
|------|--------|
| `config/devices.yaml` | Uncomment + fill `gate_controller_out` under `site` |
| `HARDWARE.md` | Update Exit Gate Gap section with findings |

### Open question

Are you planning to deploy to the real site soon, or is this a code-readiness
task (make the config complete and the code robust for when hardware is
available)?

---

## Plan 2: Add TLS/HTTPS

**Problem:** All communication is plaintext — MQTT (barrier commands), HTTP API
(transactions, payment data), and camera snapshot fetches (credentials). On a
closed LAN this is low-risk, but MQTT carries barrier-open commands.

### Scope — three channels

| Channel | Current | Target | Priority |
|---------|---------|--------|----------|
| MQTT | Plain TCP `:1883`, anonymous in sim, password-only in site | TLS `:8883` + fallback `:1883` for legacy hardware | High — carries barrier commands |
| API | Plain HTTP `:8000` | HTTPS `:8443` or behind nginx/Caddy | Medium — transaction data |
| Cameras | HTTP digest auth | HTTPS if camera supports it | Low — depends on firmware |

### Approach

**MQTT TLS (`mosquitto` + `paho-mqtt`):**

1. **Certificates:** Generate a self-signed CA + server cert (for dev/sim). For
   site, use a CA or Let's Encrypt if the broker has internet; otherwise
   self-signed with the CA cert distributed to clients.
2. **Mosquitto config:** Add a second listener on `:8883` with `cafile`,
   `certfile`, `keyfile`, `require_certificate false` (for password auth over
   TLS). Keep `:1883` for legacy devices that can't do TLS.
3. **MqttBus:** Add TLS parameters to `BrokerConfig`:
   - `tls: bool` — enable TLS
   - `ca_cert: str | None` — path to CA cert
   - `certfile: str | None`, `keyfile: str | None` — client cert (optional)
4. **Config:** Add TLS stanza under `broker` in `devices.yaml` for each
   environment. On `sim`/`e2e`, TLS is off (simpler). On `site`, TLS is on.
5. **paho integration:** In `trafix/mqtt_bus.py`, call `client.tls_set()` before
   `connect()` when `broker.tls` is true.

**API HTTPS (FastAPI/uvicorn):**

1. Add `tls_certfile` and `tls_keyfile` fields to `ApiConfig`
2. Pass them to `uvicorn.run(ssl_certfile=..., ssl_keyfile=...)` in
   `trafix/server.py`
3. Update `base_url` in config from `http://` to `https://`
4. For sim/e2e, TLS off. For site, provide cert paths.
5. The orchestrator calls the API over loopback — loopback can be HTTP even
   when TLS is on externally (bind to two interfaces or use a reverse proxy
   pattern).

**Alternative — nginx sidecar (more standard):**

- Add an nginx container that terminates TLS and reverse-proxies to the
  FastAPI app
- The Python code stays unchanged
- Requires the site to have nginx available (it already runs Laravel behind
  nginx)

### Files affected

| File | Change |
|------|--------|
| `mosquitto/config/mosquitto.conf` | Add `listener 8883` with TLS cert paths |
| `docker-compose.yml` | Mount cert directory, expose `:8883` |
| `trafix/config.py` | Add `tls` / `ca_cert` / `certfile` / `keyfile` to `BrokerConfig`; add `tls_certfile` / `tls_keyfile` to `ApiConfig` |
| `trafix/mqtt_bus.py` | Call `tls_set()` when broker config has TLS |
| `trafix/server.py` | Pass SSL cert paths to `uvicorn.run()` |
| `config/devices.yaml` | Add TLS stanzas under `broker` and `api` in `site` env |
| `cli/trafix.py` | Update any URL construction for `https://` |

### Open questions

1. **MQTT TLS:** Should we keep `:1883` open for legacy hardware (gate
   controllers that can't do TLS) or require TLS everywhere? Keeping both gives
   a migration path.
2. **API TLS:** Reverse proxy (nginx, no code change) vs. uvicorn directly
   (self-contained). Which do you prefer?
3. **Self-signed vs real CA:** For the site, should we generate self-signed
   certs for the LAN or use real certs?
