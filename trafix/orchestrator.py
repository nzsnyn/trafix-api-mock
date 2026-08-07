"""The MQTT orchestrator — the component missing from the production repo.

flow.md §2 and open question 1: something on the server subscribes to
``/GATE/event/1``, reacts to the sensor inputs, calls the LPR, asks Laravel for
a ticket, and publishes the barrier command. It is not in the codebase, is not
in ``mosquitto/`` (mode 000), and nothing in the tree so much as mentions
``outputCtrl``. Every gate opening on site comes from a binary nobody can read.

This is a clean-room replacement, reconstructed from the observed message
sequence in flow.md §5 — the timings and ordering there are the specification.

Entry, per the capture::

    1. controller  inputInfo input3=1            vehicle on the arrival loop
    2. server      /GATE/IN/1/status "welcome"
    3. controller  inputInfo input2=1            ticket button pressed
    4. server      GET .130:8090/checklpr        read the plate
    5. server      POST /api/gatein              ticket issued, printed
    6. server      /GATE/IN/1/status "thanks"
    7. server      outputCtrl relay1             BARRIER OPENS
    8. controller  inputInfo input4=1            vehicle clears the lane

Exit is not in the capture, because on site the automated path is dead. Here
the exit LPR's ``gate/out/{gate}/pos`` announcement drives it.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from trafix.config import Config, load_config
from trafix.mqtt_bus import MqttBus
from trafix.protocol import (
    INPUT_ARRIVAL_LOOP,
    INPUT_PASS_LOOP,
    INPUT_TICKET_BUTTON,
    METHOD_INPUT_INFO,
    METHOD_OUTPUT_CTRL,
    METHOD_READ_CARD,
    METHOD_TX_UART_DATA,
    STATUS_THANKS,
    STATUS_WELCOME,
    Envelope,
    gate_event_topic,
    gate_in_topic,
    gate_out_pos_topic,
    gate_out_topic,
    gate_status_topic,
    open_barrier,
    signage,
)

log = logging.getLogger("orchestrator")

# The plate the LPR reports when it saw nothing. 4 of 6 tickets on site.
NO_PLATE = ""


@dataclass
class LaneState:
    """What the orchestrator remembers about one lane."""

    occupied: bool = False
    last_ticket_at: float = 0.0
    last_ticket_code: str | None = None
    tickets_issued: int = 0


class Orchestrator:
    def __init__(
        self, config: Config, *, vehicle_id: int = 1, rfid_only: bool = False
    ) -> None:
        self.config = config
        # The gate hardware cannot tell a car from a motorcycle. On a
        # single-class site this is fixed; a mixed site needs either a
        # per-lane setting or an operator button.
        self.vehicle_id = vehicle_id
        # On-site live testing mode: react to nothing but readCard so we never
        # issue a second ticket for a real car or the ticket button.
        self.rfid_only = rfid_only

        self.bus = MqttBus(config.broker, client_id="orchestrator")
        self.http = httpx.Client(timeout=config.policies.lpr_timeout_seconds)
        self.lanes: dict[str, LaneState] = {}
        self._locks: dict[str, threading.Lock] = {}

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        for gate in self.config.controllers:
            self.lanes[gate] = LaneState()
            self._locks[gate] = threading.Lock()
            self.bus.subscribe(gate_event_topic(gate), self._make_event_handler(gate))
            log.info("watching gate %s on %s", gate, gate_event_topic(gate))

        # The exit LPR announces reads instead of being polled.
        for gate, lpr in self.config.lpr.items():
            if gate == self._entry_gate():
                continue
            self.bus.subscribe_raw(
                gate_out_pos_topic(lpr.pos_topic_gate), self._make_exit_handler(gate)
            )
            log.info(
                "watching exit reads on %s (logical gate %s)",
                gate_out_pos_topic(lpr.pos_topic_gate),
                gate,
            )

        self.bus.connect()
        self._check_dependencies()

    def stop(self) -> None:
        self.bus.disconnect()
        self.http.close()

    def _entry_gate(self) -> str:
        return "1"

    def _check_dependencies(self) -> None:
        try:
            response = self.http.get(f"{self.config.api.base_url}/api/health")
            log.info("API reachable: %s", response.json())
        except httpx.HTTPError as exc:
            log.error("API NOT reachable at %s: %s", self.config.api.base_url, exc)

        for gate, lpr in self.config.lpr.items():
            if not lpr.serves_http:
                log.warning(
                    "LPR %s serves no HTTP (as on site, §7.2) — gate %s relies on "
                    "its MQTT announcements",
                    lpr.name,
                    gate,
                )
                continue
            try:
                response = self.http.get(f"{lpr.base_url}/checklpr", timeout=2)
                response.raise_for_status()
                log.info("LPR %s reachable at %s", lpr.name, lpr.base_url)
            except httpx.HTTPError as exc:
                log.error("LPR %s NOT reachable at %s: %s", lpr.name, lpr.base_url, exc)

    # -- entry lane --------------------------------------------------------

    def _make_event_handler(self, gate: str):
        def handler(_topic: str, message: Envelope) -> None:
            if self.rfid_only and message.method != METHOD_READ_CARD:
                log.debug(
                    "gate %s: rfid-only, ignoring %s", gate, message.method
                )
                return
            if message.method == METHOD_INPUT_INFO:
                with self._locks[gate]:
                    self._on_inputs(gate, message)
            elif message.method == METHOD_READ_CARD:
                with self._locks[gate]:
                    self._on_card(gate, message)
            elif message.method in (METHOD_TX_UART_DATA, METHOD_OUTPUT_CTRL):
                log.debug("gate %s: controller acked %s", gate, message.method)
            else:
                log.debug("gate %s: %s", gate, message.method)

        return handler

    def _on_inputs(self, gate: str, message: Envelope) -> None:
        lane = self.lanes[gate]
        arrival = _as_int(message.get(INPUT_ARRIVAL_LOOP))
        button = _as_int(message.get(INPUT_TICKET_BUTTON))
        passed = _as_int(message.get(INPUT_PASS_LOOP))

        if arrival and not lane.occupied:
            lane.occupied = True
            log.info("gate %s: vehicle arrived", gate)
            self.bus.publish_raw(gate_status_topic(gate), signage(STATUS_WELCOME))

        if button:
            self._handle_button(gate, message)

        if passed:
            if lane.occupied:
                log.info("gate %s: vehicle cleared the lane", gate)
            lane.occupied = False

    def _handle_button(self, gate: str, message: Envelope) -> None:
        lane = self.lanes[gate]
        now = time.monotonic()

        # Drivers press twice when the printer is slow. A second ticket for one
        # car leaves an orphan record that can never be checked out.
        window = self.config.policies.button_debounce_seconds
        if lane.last_ticket_code and (now - lane.last_ticket_at) < window:
            log.info(
                "gate %s: repeat press within %.0fs, ignoring (ticket %s stands)",
                gate,
                window,
                lane.last_ticket_code,
            )
            return

        log.info("gate %s: TICKET BUTTON — reading plate", gate)
        plate, image_url = self._read_plate(gate)

        ticket = self._request_ticket(
            gate=gate,
            plate=plate,
            image_url=image_url,
            serial_no=message.serial_no,
        )
        if ticket is None:
            # The API failed. Do not open: without a ticket the driver has no
            # way to check out, and an unrecorded car is worse than a delay.
            log.error("gate %s: no ticket issued, barrier stays shut", gate)
            self.bus.publish_raw(gate_status_topic(gate), signage(STATUS_THANKS))
            return

        lane.last_ticket_code = ticket
        lane.last_ticket_at = now
        lane.tickets_issued += 1

        self.bus.publish_raw(gate_status_topic(gate), signage(STATUS_THANKS))
        self._open(gate)

    def _on_card(self, gate: str, message: Envelope) -> None:
        """An RFID tag was presented. Resolve it to a member and open the gate.

        A member entry creates no paper ticket, so there is no orphan risk from
        repeat taps, but a debounce still stops a double-tap opening twice.
        """
        card_no = str(message.get("cardNo") or "").strip()
        if not card_no:
            log.warning("gate %s: readCard with no cardNo", gate)
            return

        lane = self.lanes[gate]
        now = time.monotonic()
        window = self.config.policies.button_debounce_seconds
        if lane.last_ticket_code and (now - lane.last_ticket_at) < window:
            log.info(
                "gate %s: repeat card tap within %.0fs, ignoring",
                gate,
                window,
            )
            return

        member = self._request_member_entry(gate, card_no, message.serial_no)
        if member is None:
            log.warning("gate %s: member entry refused for card %s", gate, card_no)
            return

        lane.last_ticket_code = member.get("kode_tiket")
        lane.last_ticket_at = now
        lane.tickets_issued += 1

        log.info(
            "gate %s: member %s entered on card %s (ticket %s)",
            gate,
            member.get("name"),
            card_no,
            member.get("kode_tiket"),
        )
        self.bus.publish_raw(gate_status_topic(gate), signage(STATUS_THANKS))
        self._open(gate)

    def _request_member_entry(
        self, gate: str, card_no: str, serial_no: str
    ) -> dict | None:
        """POST /api/gatein/card. None when the card is refused or the API fails."""
        try:
            response = self.http.post(
                f"{self.config.api.base_url}/api/gatein/card",
                json={
                    "gate": gate,
                    "card_no": card_no,
                    "serialNo": serial_no,
                    "vehicle_id": self.vehicle_id,
                },
                timeout=10,
            )
            if response.status_code == 404:
                log.info("gate %s: card %s not a member", gate, card_no)
                return None
            if response.status_code == 403:
                log.warning(
                    "gate %s: card %s expired: %s",
                    gate,
                    card_no,
                    response.json().get("message"),
                )
                return None
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("gate %s: /api/gatein/card failed: %s", gate, exc)
            return None

        if not payload.get("kode_tiket"):
            log.error("gate %s: /api/gatein/card returned no ticket: %s", gate, payload)
            return None

        log.info("gate %s: member entry accepted for card %s", gate, card_no)
        return payload

    def _read_plate(self, gate: str) -> tuple[str, str]:
        """Ask the entry LPR what it can see.

        A failure here is never fatal: on site 4 of 6 tickets recorded no plate
        at all and the gate still worked. The ticket code is what gets the
        driver out, not the plate.
        """
        try:
            lpr = self.config.lpr_for(gate)
        except Exception:
            log.warning("gate %s has no LPR configured", gate)
            return NO_PLATE, ""

        if not lpr.serves_http:
            log.warning("gate %s: LPR serves no HTTP, issuing a ticket with no plate", gate)
            return NO_PLATE, ""

        attempts = max(1, self.config.policies.lpr_retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                response = self.http.get(f"{lpr.base_url}/checklpr")
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning(
                    "gate %s: checklpr attempt %s/%s failed: %s",
                    gate,
                    attempt,
                    attempts,
                    exc,
                )
                continue

            plate = str(data.get("plate_num") or "").strip()
            image_url = str(data.get("url_gambar") or "").strip()
            if plate:
                log.info("gate %s: plate %s", gate, plate)
            else:
                log.warning("gate %s: LPR read no plate", gate)
            return plate, image_url

        log.error("gate %s: LPR unreachable, issuing a ticket with no plate", gate)
        return NO_PLATE, ""

    def _request_ticket(
        self, *, gate: str, plate: str, image_url: str, serial_no: str
    ) -> str | None:
        """POST /api/gatein.

        On site this call goes over loopback, which is why it never shows up in
        a capture of the external interface (flow.md §3).
        """
        try:
            response = self.http.post(
                f"{self.config.api.base_url}/api/gatein",
                json={
                    "gate": gate,
                    "vehicle_id": self.vehicle_id,
                    "plate_num": plate,
                    "url_gambar": image_url,
                    "serialNo": serial_no,
                },
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("gate %s: /api/gatein failed: %s", gate, exc)
            return None

        code = payload.get("kode_tiket")
        if not code:
            log.error("gate %s: /api/gatein returned no ticket: %s", gate, payload)
            return None

        log.info("gate %s: ticket %s issued", gate, code)
        return str(code)

    # -- exit lane ---------------------------------------------------------

    def _make_exit_handler(self, gate: str):
        def handler(_topic: str, payload: str) -> None:
            try:
                data = json.loads(payload)
            except json.JSONDecodeError:
                log.warning("gate %s: undecodable exit announcement: %r", gate, payload)
                return

            plate = str(data.get("plate_num") or "").strip()
            image_url = str(data.get("url_gambar") or "").strip()

            if not plate:
                log.warning("gate %s: exit LPR read no plate, cannot resolve a ticket", gate)
                return

            log.info("gate %s: exit read %s", gate, plate)
            self._settle_by_plate(gate, plate, image_url)

        return handler

    def _settle_by_plate(self, gate: str, plate: str, image_url: str) -> None:
        """Automated exit through the production ``gateoutcard`` contract.

        On site the exit gate controller itself calls ``PUT /api/lpr/gateoutcard``
        (``GateoutController::GateOutRfidLpr`` :1603) with the scanned RFID/ticket;
        the backend answers ``success_member``/``success_ticket`` and the device
        firmware raises the barrier. The simulator has no device, so the
        orchestrator plays it: it quotes the fee first (only free sessions are
        released — a chargeable ticket is left for the cashier), settles through
        the real endpoint, then raises the exit barrier on success.
        """
        quote = self._quote_exit(gate, plate)
        if quote is None:
            return
        code, total = quote

        if total > 0:
            log.info(
                "gate %s: %s owes %s — waiting for the cashier",
                gate,
                plate,
                total,
            )
            return

        try:
            response = self.http.put(
                f"{self.config.api.base_url}/api/lpr/gateoutcard",
                json={
                    "card": code,
                    "plate_num": plate,
                    "url_gambar": image_url,
                    "gate_out": gate,
                },
                timeout=10,
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("gate %s: /api/lpr/gateoutcard failed: %s", gate, exc)
            return

        status = payload.get("status")
        if status in ("success_member", "success_ticket"):
            log.info(
                "gate %s: %s released (%s), raising the barrier",
                gate,
                plate,
                status,
            )
            # The device firmware opens on success_*; stand in for it here.
            self._open(gate, exit_lane=True)
            return

        log.warning(
            "gate %s: automated exit refused for %s: %s", gate, plate, status
        )

    def _quote_exit(self, gate: str, plate: str) -> tuple[str, float] | None:
        """The cashier's own quote for the plate: (transaction code, fee)."""
        try:
            response = self.http.post(
                f"{self.config.api.base_url}/api/gateout/detailtransaction",
                json={"transaction_code": "", "police_number": plate},
                timeout=10,
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("gate %s: exit quote failed: %s", gate, exc)
            return None

        if payload.get("status") != "success":
            log.warning(
                "gate %s: no open transaction for %s (%s)",
                gate,
                plate,
                payload.get("status"),
            )
            return None

        data = payload.get("data") or {}
        code = data.get("transaction_code")
        if not code:
            log.warning("gate %s: quote carried no transaction code", gate)
            return None
        return str(code), float(data.get("total") or 0)

    # -- barrier -----------------------------------------------------------

    def _open(self, gate: str, *, exit_lane: bool = False) -> None:
        controller = self.config.controller_for(gate)
        topic = gate_out_topic(gate) if exit_lane else gate_in_topic(gate)
        self.bus.publish(
            topic,
            open_barrier(
                controller.serial_no,
                pulse_ms=self.config.policies.barrier_pulse_ms,
                beep_ms=self.config.policies.barrier_beep_ms,
            ),
        )
        log.info("gate %s: 🚧 barrier command sent (%s)", gate, topic)


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trafix MQTT orchestrator (the component missing from the site)"
    )
    parser.add_argument("--env", default=None)
    parser.add_argument(
        "--vehicle-id",
        type=int,
        default=1,
        help="vehicle class to record for every entry (1=Motor, 2=Mobil)",
    )
    parser.add_argument("--log-level", default="info")
    parser.add_argument(
        "--rfid-only",
        action="store_true",
        help="react only to readCard; ignore arrival and ticket-button events "
        "(safe for on-site testing alongside the live system)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)-12s | %(message)s",
    )

    config = load_config(args.env)
    orchestrator = Orchestrator(
        config, vehicle_id=args.vehicle_id, rfid_only=args.rfid_only
    )
    orchestrator.start()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    log.info("orchestrator running — Ctrl-C to stop")
    try:
        stop.wait()
    finally:
        orchestrator.stop()


if __name__ == "__main__":
    main()
