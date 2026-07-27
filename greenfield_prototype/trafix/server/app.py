"""The parking server: MQTT to the terminals, HTTP to the cameras.

This is the production process. It owns the database and both lanes, and it is
the only component that decides whether a barrier opens.
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
from typing import Callable

from trafix.config import Config, load_config
from trafix.db import Database
from trafix.envelope import (
    EVT_CHECKOUT_REQUEST,
    EVT_PAYMENT_SETTLED,
    EVT_TICKET_PRINTED,
    EVT_TICKET_REQUEST,
    EVT_VEHICLE_PASSED,
    Envelope,
    cmd_topic,
    evt_topic,
    state_topic,
)
from trafix.lpr_client import LprClient
from trafix.mqtt_bus import STATE_ONLINE, MqttBus
from trafix.server.checkin import CheckinService
from trafix.server.checkout import CheckoutService

log = logging.getLogger("server")

SOURCE = "server"


class ParkingServer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.db = Database(config.database)
        self.bus = MqttBus(config.broker, client_id="server")

        self.lpr = {
            lane: LprClient(config.lpr_for(lane), config.policies) for lane in ("in", "out")
        }
        # Terminal availability, kept up to date from the retained state topic.
        self.terminal_online: dict[str, bool] = {"in": False, "out": False}

        self.checkin = CheckinService(
            lane="in",
            config=config,
            db=self.db,
            lpr=self.lpr["in"],
            publish=self._publish_command,
        )
        self.checkout = CheckoutService(
            lane="out",
            config=config,
            db=self.db,
            lpr=self.lpr["out"],
            publish=self._publish_command,
        )

        # Each lane serialises its own events: two drivers on the same lane
        # cannot interleave, and the two lanes never block each other.
        self._lane_locks = {"in": threading.Lock(), "out": threading.Lock()}

        self._handlers: dict[str, dict[str, Callable[[Envelope], object]]] = {
            "in": {
                EVT_TICKET_REQUEST: self.checkin.on_ticket_request,
                EVT_TICKET_PRINTED: self.checkin.on_ticket_printed,
                EVT_VEHICLE_PASSED: self.checkin.on_vehicle_passed,
            },
            "out": {
                EVT_CHECKOUT_REQUEST: self.checkout.on_checkout_request,
                EVT_PAYMENT_SETTLED: self.checkout.on_payment_settled,
                EVT_VEHICLE_PASSED: self.checkout.on_vehicle_passed,
            },
        }

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        for lane in ("in", "out"):
            self.bus.subscribe(evt_topic(lane), self._make_event_handler(lane))
            self.bus.subscribe_raw(state_topic(lane), self._make_state_handler(lane))

        self.bus.connect()
        log.info(
            "server up (env=%s, db=%s), lanes in/out listening",
            self.config.env,
            self.config.database,
        )
        self._report_camera_health()

    def stop(self) -> None:
        log.info("server shutting down")
        self.bus.disconnect()
        for client in self.lpr.values():
            client.close()
        self.db.close()

    def _report_camera_health(self) -> None:
        for lane, client in self.lpr.items():
            reachable = client.health()
            log.info(
                "camera %s (%s): %s",
                client.config.name,
                client.config.base_url,
                "reachable" if reachable else "NOT REACHABLE",
            )
            if not reachable:
                log.warning(
                    "lane %s will refuse or flag entries until the camera answers", lane
                )

    # -- MQTT plumbing -----------------------------------------------------

    def _publish_command(self, lane: str, message: Envelope) -> None:
        if not self.terminal_online.get(lane, False):
            log.warning(
                "lane %s terminal is offline; publishing %s anyway (broker will queue)",
                lane,
                message.type,
            )
        self.bus.publish(cmd_topic(lane), message)

    def _make_state_handler(self, lane: str) -> Callable[[str, str], None]:
        """Track terminal availability from the retained state topic.

        The payload is a bare ``online``/``offline`` string rather than an
        envelope, because an MQTT last will has to be a fixed payload fixed at
        connect time.
        """

        def handler(_topic: str, payload: str) -> None:
            online = payload.strip() == STATE_ONLINE
            if self.terminal_online.get(lane) == online:
                return
            self.terminal_online[lane] = online
            log.info("lane %s terminal is %s", lane, "online" if online else "OFFLINE")
            self.db.log_event(
                source=SOURCE,
                type="terminal_online" if online else "terminal_offline",
                lane=lane,
            )

        return handler

    def _make_event_handler(self, lane: str) -> Callable[[str, Envelope], None]:
        def handler(_topic: str, message: Envelope) -> None:
            action = self._handlers[lane].get(message.type)
            if action is None:
                log.warning("lane %s: unhandled event %s", lane, message.type)
                return
            with self._lane_locks[lane]:
                try:
                    action(message)
                except Exception:
                    log.exception(
                        "lane %s: failed handling %s from %s",
                        lane,
                        message.type,
                        message.device_id,
                    )
                    self.db.log_event(
                        source=SOURCE,
                        type="handler_error",
                        lane=lane,
                        detail={"event": message.type, "device": message.device_id},
                    )

        return handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Trafix parking server")
    parser.add_argument("--env", default=None, help="config environment override")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)-9s | %(message)s",
    )

    config = load_config(args.env)
    server = ParkingServer(config)
    server.start()

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        server.stop()


if __name__ == "__main__":
    main()
