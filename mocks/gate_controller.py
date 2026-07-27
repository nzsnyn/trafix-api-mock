"""Mock gate controller — the relay + thermal printer board at ``.204``.

Speaks exactly what the real board speaks: publishes ``inputInfo`` sensor
reports and command acknowledgements on ``/GATE/event/{gate}``, subscribes to
``/GATE/IN/{gate}`` for ``txUartData`` (print) and ``outputCtrl`` (relay).

The ``trafix/sim/...`` topic is simulator control and has no counterpart on
real hardware. It is how the CLI makes this board behave as if a vehicle had
driven up and a driver had pressed the button — so every event the server sees
still originates from the controller.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time

from trafix.config import load_config
from trafix.escpos import render_ticket
from trafix.mqtt_bus import MqttBus
from trafix.protocol import (
    INPUT_ARRIVAL_LOOP,
    INPUT_PASS_LOOP,
    INPUT_TICKET_BUTTON,
    METHOD_OUTPUT_CTRL,
    METHOD_TX_UART_DATA,
    Envelope,
    ack,
    gate_event_topic,
    gate_in_topic,
    gate_out_topic,
    input_info,
)

log = logging.getLogger("controller")

# Simulator control topic and commands.
SIM_ARRIVE = "arrive"
SIM_PRESS = "press"
SIM_PASS = "pass"
SIM_CYCLE = "cycle"  # arrive, press, then pass once the barrier opens
SIM_SET = "set"


def sim_topic(gate: str) -> str:
    return f"trafix/sim/controller/{gate}"


class GateControllerMock:
    """One relay board serving one lane."""

    def __init__(
        self,
        bus: MqttBus,
        *,
        gate: str,
        serial_no: str,
        command_topic: str,
        auto_pass: bool = True,
        pass_delay: float = 1.0,
    ) -> None:
        self.bus = bus
        self.gate = gate
        self.serial_no = serial_no
        self.command_topic = command_topic
        self.auto_pass = auto_pass
        self.pass_delay = pass_delay

        # Sensor state, reported cumulatively the way the real board does:
        # a button press message still carries input3=1 if the car is present.
        self.inputs = {INPUT_ARRIVAL_LOOP: 0, INPUT_TICKET_BUTTON: 0, INPUT_PASS_LOOP: 0}
        self.last_ticket: str | None = None
        self._timers: list[threading.Timer] = []
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.bus.subscribe(self.command_topic, self._on_command)
        self.bus.subscribe(sim_topic(self.gate), self._on_sim)
        self.bus.connect()
        log.info(
            "[gate %s] controller %s online, listening on %s",
            self.gate,
            self.serial_no,
            self.command_topic,
        )

    def stop(self) -> None:
        with self._lock:
            for timer in self._timers:
                timer.cancel()
            self._timers.clear()
        self.bus.disconnect()

    def _later(self, delay: float, action) -> None:
        if delay <= 0:
            action()
            return
        timer = threading.Timer(delay, action)
        timer.daemon = True
        with self._lock:
            self._timers = [t for t in self._timers if t.is_alive()]
            self._timers.append(timer)
        timer.start()

    # -- outgoing ----------------------------------------------------------

    def _report_inputs(self) -> None:
        """Publish the current sensor state to the server."""
        self.bus.publish(
            gate_event_topic(self.gate),
            input_info(self.serial_no, **self.inputs),
        )

    def _ack(self, method: str) -> None:
        self.bus.publish(gate_event_topic(self.gate), ack(self.serial_no, method))

    # -- simulator control -------------------------------------------------

    def _on_sim(self, _topic: str, message: Envelope) -> None:
        command = message.method

        if command == SIM_ARRIVE:
            log.info("[gate %s] vehicle on the arrival loop", self.gate)
            self.inputs[INPUT_ARRIVAL_LOOP] = 1
            self.inputs[INPUT_PASS_LOOP] = 0
            self._report_inputs()

        elif command == SIM_PRESS:
            log.info("[gate %s] TICKET BUTTON pressed", self.gate)
            self.inputs[INPUT_TICKET_BUTTON] = 1
            self._report_inputs()
            # The button is momentary: it releases straight away, but the
            # arrival loop stays occupied until the car moves off.
            self.inputs[INPUT_TICKET_BUTTON] = 0

        elif command == SIM_PASS:
            self._vehicle_passed()

        elif command == SIM_CYCLE:
            log.info("[gate %s] full arrival cycle", self.gate)
            self.inputs[INPUT_ARRIVAL_LOOP] = 1
            self.inputs[INPUT_PASS_LOOP] = 0
            self._report_inputs()
            self._later(0.4, lambda: self._on_sim("", Envelope(SIM_PRESS, self.serial_no)))

        elif command == SIM_SET:
            if "auto_pass" in (message.data or {}):
                self.auto_pass = bool(message.get("auto_pass"))
            if "pass_delay" in (message.data or {}):
                self.pass_delay = max(0.0, float(message.get("pass_delay")))
            log.info(
                "[gate %s] auto_pass=%s pass_delay=%s",
                self.gate,
                self.auto_pass,
                self.pass_delay,
            )

        else:
            log.warning("[gate %s] unknown sim command %s", self.gate, command)

    def _vehicle_passed(self) -> None:
        log.info("[gate %s] vehicle cleared the lane", self.gate)
        self.inputs[INPUT_ARRIVAL_LOOP] = 0
        self.inputs[INPUT_PASS_LOOP] = 1
        self._report_inputs()
        # The pass loop clears a moment later.
        self.inputs[INPUT_PASS_LOOP] = 0

    # -- incoming commands -------------------------------------------------

    def _on_command(self, _topic: str, message: Envelope) -> None:
        if message.method == METHOD_TX_UART_DATA:
            self._print(message)
        elif message.method == METHOD_OUTPUT_CTRL:
            self._actuate(message)
        else:
            log.warning("[gate %s] unknown command %s", self.gate, message.method)

    def _print(self, message: Envelope) -> None:
        blocks = message.data if isinstance(message.data, list) else []
        try:
            text = render_ticket(blocks)
        except ValueError as exc:
            log.error("[gate %s] undecodable print payload: %s", self.gate, exc)
            self._ack(METHOD_TX_UART_DATA)
            return

        self.last_ticket = text
        log.info("[gate %s] PRINTING:", self.gate)
        for line in text.split("\n"):
            log.info("[gate %s]   | %s", self.gate, line)
        self._ack(METHOD_TX_UART_DATA)

    def _actuate(self, message: Envelope) -> None:
        data = message.data if isinstance(message.data, dict) else {}
        relays = {k: v for k, v in data.items() if k.startswith("relay")}
        for relay, value in relays.items():
            duration = value[1] if isinstance(value, list) and len(value) > 1 else 0
            log.info(
                "[gate %s] 🚧 %s PULSED for %sms — BARRIER OPEN",
                self.gate,
                relay,
                duration,
            )
        if "beepOut" in data:
            log.info("[gate %s] beep", self.gate)

        self._ack(METHOD_OUTPUT_CTRL)

        if relays and self.auto_pass:
            self._later(self.pass_delay, self._vehicle_passed)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock gate controller board")
    parser.add_argument("--gate", required=True, help="gate number, e.g. 1 or 2")
    parser.add_argument("--env", default=None)
    parser.add_argument(
        "--exit-lane",
        action="store_true",
        help="listen on /GATE/OUT/{gate} instead of /GATE/IN/{gate}",
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)-10s | %(message)s",
    )

    config = load_config(args.env)
    controller = config.controller_for(args.gate)
    topic = (
        gate_out_topic(args.gate) if args.exit_lane else gate_in_topic(args.gate)
    )

    bus = MqttBus(config.broker, client_id=f"controller-{args.gate}")
    mock = GateControllerMock(
        bus,
        gate=args.gate,
        serial_no=controller.serial_no,
        command_topic=topic,
    )
    mock.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("[gate %s] shutting down", args.gate)
    finally:
        mock.stop()


if __name__ == "__main__":
    main()
