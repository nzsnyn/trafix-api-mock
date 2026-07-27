"""Mock manless terminal: button, ticket printer, barcode scanner, barrier.

Speaks only MQTT, exactly as the real terminal will. The extra ``sim`` topic is
how the CLI makes it act — a simulated driver pressing the button — so every
event the server sees still originates from the terminal itself.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time

from trafix.config import load_config
from trafix.envelope import (
    CMD_CHECKOUT_RESULT,
    CMD_OPEN_GATE,
    CMD_PRINT_TICKET,
    CMD_REJECT,
    CMD_SHOW_MESSAGE,
    EVT_CHECKOUT_REQUEST,
    EVT_PAYMENT_SETTLED,
    EVT_TICKET_PRINTED,
    EVT_TICKET_REQUEST,
    EVT_VEHICLE_PASSED,
    SIM_PRESS_BUTTON,
    SIM_SCAN_TICKET,
    SIM_SET_BEHAVIOUR,
    Envelope,
    cmd_topic,
    evt_topic,
    sim_topic,
    state_topic,
)
from trafix.mqtt_bus import STATE_OFFLINE, STATE_ONLINE, MqttBus

log = logging.getLogger("manless")


class Behaviour:
    """Which steps the terminal performs by itself, and how slowly.

    Turning one of these off lets a test leave the flow half-finished, which is
    how the server's timeout paths get exercised.
    """

    def __init__(self) -> None:
        self.auto_print = True
        self.auto_pay = True
        self.auto_pass = True
        self.print_delay = 0.4
        self.pay_delay = 0.8
        self.pass_delay = 1.0

    def apply(self, payload: dict) -> None:
        for key in ("auto_print", "auto_pay", "auto_pass"):
            if key in payload:
                setattr(self, key, bool(payload[key]))
        for key in ("print_delay", "pay_delay", "pass_delay"):
            if key in payload:
                setattr(self, key, max(0.0, float(payload[key])))

    def snapshot(self) -> dict:
        return {
            "auto_print": self.auto_print,
            "auto_pay": self.auto_pay,
            "auto_pass": self.auto_pass,
            "print_delay": self.print_delay,
            "pay_delay": self.pay_delay,
            "pass_delay": self.pass_delay,
        }


class ManlessTerminal:
    def __init__(self, bus: MqttBus, device_id: str, lane: str) -> None:
        self.bus = bus
        self.device_id = device_id
        self.lane = lane
        self.behaviour = Behaviour()
        self.last_ticket: dict | None = None
        self._timers: list[threading.Timer] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        self.bus.subscribe(cmd_topic(self.lane), self._on_command)
        self.bus.subscribe(sim_topic(self.lane), self._on_sim)
        self.bus.connect()
        self.bus.publish_raw(state_topic(self.lane), STATE_ONLINE, retain=True)
        log.info("[%s] online on lane %s", self.device_id, self.lane)

    def stop(self) -> None:
        with self._lock:
            for timer in self._timers:
                timer.cancel()
            self._timers.clear()
        self.bus.publish_raw(state_topic(self.lane), STATE_OFFLINE, retain=True)
        time.sleep(0.2)  # let the retained state reach the broker before we go
        self.bus.disconnect()

    # -- outgoing events ---------------------------------------------------

    def _emit(
        self, type: str, payload: dict | None = None, correlation_id: str | None = None
    ) -> None:
        self.bus.publish(
            evt_topic(self.lane),
            Envelope(
                type=type,
                device_id=self.device_id,
                payload=payload or {},
                correlation_id=correlation_id,
            ),
        )

    def _later(self, delay: float, action) -> None:
        """Run ``action`` after ``delay`` without blocking the network loop."""
        if delay <= 0:
            action()
            return
        timer = threading.Timer(delay, action)
        timer.daemon = True
        with self._lock:
            self._timers = [t for t in self._timers if t.is_alive()]
            self._timers.append(timer)
        timer.start()

    # -- simulator control -------------------------------------------------

    def _on_sim(self, _topic: str, message: Envelope) -> None:
        if message.type == SIM_PRESS_BUTTON:
            log.info("[%s] BUTTON pressed", self.device_id)
            self._emit(EVT_TICKET_REQUEST, {"trigger": "button"})

        elif message.type == SIM_SCAN_TICKET:
            barcode = str(message.get("barcode", "")).strip()
            lost = bool(message.get("lost_ticket", False))
            if lost:
                log.info("[%s] LOST TICKET declared", self.device_id)
            else:
                log.info("[%s] SCANNED barcode %s", self.device_id, barcode)
            self._emit(EVT_CHECKOUT_REQUEST, {"barcode": barcode, "lost_ticket": lost})

        elif message.type == SIM_SET_BEHAVIOUR:
            self.behaviour.apply(message.payload)
            log.info("[%s] behaviour -> %s", self.device_id, self.behaviour.snapshot())

        else:
            log.warning("[%s] unknown sim command %s", self.device_id, message.type)

    # -- incoming commands -------------------------------------------------

    def _on_command(self, _topic: str, message: Envelope) -> None:
        handler = {
            CMD_PRINT_TICKET: self._print_ticket,
            CMD_OPEN_GATE: self._open_gate,
            CMD_CHECKOUT_RESULT: self._checkout_result,
            CMD_REJECT: self._reject,
            CMD_SHOW_MESSAGE: self._show_message,
        }.get(message.type)

        if handler is None:
            log.warning("[%s] unknown command %s", self.device_id, message.type)
            return
        handler(message)

    def _print_ticket(self, message: Envelope) -> None:
        self.last_ticket = dict(message.payload)
        log.info(
            "[%s] PRINTING ticket %s | plate %s | barcode %s",
            self.device_id,
            message.get("ticket_no"),
            message.get("plate"),
            message.get("barcode"),
        )
        if message.get("image_url"):
            log.info("[%s]   snapshot: %s", self.device_id, message.get("image_url"))

        if not self.behaviour.auto_print:
            log.info("[%s] auto_print off, not confirming", self.device_id)
            return

        self._later(
            self.behaviour.print_delay,
            lambda: self._emit(
                EVT_TICKET_PRINTED,
                {"ticket_no": message.get("ticket_no"), "barcode": message.get("barcode")},
                correlation_id=message.msg_id,
            ),
        )

    def _checkout_result(self, message: Envelope) -> None:
        if message.get("error"):
            log.warning(
                "[%s] CHECKOUT REFUSED: %s", self.device_id, message.get("error")
            )
            return

        fee = message.get("fee", 0)
        log.info(
            "[%s] CHECKOUT %s | in %s / out %s | match=%s | %s min | fee %s",
            self.device_id,
            message.get("ticket_no"),
            message.get("plate_in"),
            message.get("plate_out"),
            message.get("plate_match"),
            message.get("duration_minutes"),
            fee,
        )

        if not self.behaviour.auto_pay:
            log.info("[%s] auto_pay off, waiting for a human", self.device_id)
            return

        self._later(
            self.behaviour.pay_delay,
            lambda: self._emit(
                EVT_PAYMENT_SETTLED,
                {
                    "ticket_no": message.get("ticket_no"),
                    "barcode": message.get("barcode"),
                    "amount": fee,
                    "method": "cash",
                },
                correlation_id=message.msg_id,
            ),
        )

    def _open_gate(self, message: Envelope) -> None:
        log.info("[%s] BARRIER OPEN (ticket %s)", self.device_id, message.get("ticket_no"))
        if not self.behaviour.auto_pass:
            log.info("[%s] auto_pass off, vehicle stays on the loop", self.device_id)
            return

        def passed() -> None:
            log.info("[%s] vehicle passed, BARRIER CLOSED", self.device_id)
            self._emit(
                EVT_VEHICLE_PASSED,
                {"ticket_no": message.get("ticket_no"), "barcode": message.get("barcode")},
                correlation_id=message.msg_id,
            )

        self._later(self.behaviour.pass_delay, passed)

    def _reject(self, message: Envelope) -> None:
        log.warning(
            "[%s] REJECTED: %s", self.device_id, message.get("reason", "no reason given")
        )

    def _show_message(self, message: Envelope) -> None:
        log.info("[%s] DISPLAY: %s", self.device_id, message.get("text", ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock manless terminal")
    parser.add_argument("--lane", required=True, choices=["in", "out"])
    parser.add_argument("--env", default=None, help="config environment override")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    config = load_config(args.env)
    device = config.manless_for(args.lane)

    bus = MqttBus(
        config.broker,
        client_id=device.name,
        will_topic=state_topic(args.lane),
        will_payload=STATE_OFFLINE,
    )
    terminal = ManlessTerminal(bus, device_id=device.name, lane=args.lane)
    terminal.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("[%s] shutting down", device.name)
    finally:
        terminal.stop()


if __name__ == "__main__":
    main()
