"""Publishes gate commands over MQTT.

Implements the :class:`trafix.service.Publisher` protocol, so the business
logic never imports a broker client.
"""

from __future__ import annotations

import logging

from trafix.config import Config
from trafix.mqtt_bus import MqttBus
from trafix.protocol import (
    gate_in_topic,
    gate_out_topic,
    open_barrier,
    print_ticket,
)

log = logging.getLogger("publisher")


class MqttPublisher:
    def __init__(self, config: Config, client_id: str = "api") -> None:
        self.config = config
        self.bus = MqttBus(config.broker, client_id=client_id)

    def start(self) -> None:
        self.bus.connect()

    def stop(self) -> None:
        self.bus.disconnect()

    def _serial_for(self, gate: str) -> str:
        try:
            return self.config.controller_for(gate).serial_no
        except Exception:
            # An exit controller may not be configured — on site there is no
            # such device at all (flow.md §7.6). Publish anyway with an empty
            # serial so the message is visible to whoever is listening.
            log.warning("no controller configured for gate %s", gate)
            return ""

    def print_ticket(self, gate: str, blocks: list[dict], message_id: str) -> None:
        self.bus.publish(
            gate_in_topic(gate),
            print_ticket(self._serial_for(gate), blocks, message_id),
        )

    def open_barrier(self, gate: str, *, exit_lane: bool = False) -> None:
        topic = gate_out_topic(gate) if exit_lane else gate_in_topic(gate)
        self.bus.publish(
            topic,
            open_barrier(
                self._serial_for(gate),
                pulse_ms=self.config.policies.barrier_pulse_ms,
                beep_ms=self.config.policies.barrier_beep_ms,
            ),
        )
        log.info("barrier command sent to %s", topic)
