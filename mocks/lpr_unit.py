"""Mock LPR unit — the plate-reading cameras at ``.130`` and ``.149``.

The two behave differently on site, and both behaviours are reproduced here:

* **Entry (.130)** serves ``GET :8090/checklpr``, answering
  ``{"plate_num": ..., "url_gambar": ...}``, and serves the image at that URL.
  It also subscribes to ``/GATE/IN/{gate}/status`` to drive its signage.
* **Exit (.149)** publishes ``gate/out/{gate}/pos`` when it reads a plate. On
  site its HTTP server accepts no connections at all (flow.md §7.2), which is
  reproduced when ``serves_http`` is false in the config.

``/mock/*`` endpoints are simulator control and have no counterpart on the real
device.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, HTTPException, Response

from mocks.plate_image import random_plate, render_plate_jpeg
from trafix.config import load_config
from trafix.mqtt_bus import MqttBus
from trafix.protocol import (
    Envelope,
    gate_out_pos_topic,
    gate_status_topic,
)

log = logging.getLogger("lpr")

# Failure modes, for exercising the server's error handling.
MODE_OK = "ok"
MODE_NO_PLATE = "no_plate"
MODE_ERROR = "error"
MODE_TIMEOUT = "timeout"
MODES = (MODE_OK, MODE_NO_PLATE, MODE_ERROR, MODE_TIMEOUT)

IMAGE_RETENTION = 200

# Simulator control topic for the exit unit, which has no HTTP surface.
def sim_topic(gate: str) -> str:
    return f"trafix/sim/lpr/{gate}"


SIM_READ_PLATE = "read_plate"


class LprState:
    def __init__(self, name: str, gate: str, public_url: str, seed: int | None) -> None:
        self.name = name
        self.gate = gate
        self.public_url = public_url.rstrip("/")
        self.rng = random.Random(seed)
        self.lock = threading.Lock()

        self.mode = MODE_OK
        self.queued: deque[str] = deque()
        self.images: dict[str, bytes] = {}
        self.image_order: deque[str] = deque()
        self.last_plate: str | None = None
        self.reads = 0
        self.signage = "-"

    def take_plate(self) -> str:
        if self.queued:
            return self.queued.popleft()
        return random_plate(self.rng)

    def capture(self, plate: str) -> str:
        """Render and store a snapshot, returning its public URL."""
        now = datetime.now(timezone.utc)
        image = render_plate_jpeg(
            plate,
            lane=self.gate,
            device=self.name,
            captured_at=now,
            confidence=round(self.rng.uniform(0.82, 0.99), 2),
        )
        image_id = f"{plate}_{uuid.uuid4().hex[:8]}"
        self.images[image_id] = image
        self.image_order.append(image_id)
        while len(self.image_order) > IMAGE_RETENTION:
            self.images.pop(self.image_order.popleft(), None)
        self.last_plate = plate
        self.reads += 1
        return f"{self.public_url}/image/{image_id}.jpg"


def create_app(state: LprState) -> FastAPI:
    app = FastAPI(title=f"Mock LPR {state.name}", version="1.0")

    @app.get("/checklpr")
    def checklpr() -> Response:
        """The endpoint the orchestrator polls at the moment of a button press.

        Response shape taken verbatim from the capture (flow.md §5 step 4).
        """
        with state.lock:
            mode = state.mode

        if mode == MODE_TIMEOUT:
            log.warning("[%s] simulating a hung unit", state.name)
            time.sleep(30)

        if mode == MODE_ERROR:
            log.warning("[%s] simulating a fault", state.name)
            raise HTTPException(status_code=503, detail="lpr unavailable")

        if mode == MODE_NO_PLATE:
            # 4 of 6 tickets on site recorded no plate at all (§7.7). This is
            # what that looks like on the wire.
            log.warning("[%s] no plate detected", state.name)
            return _json({"plate_num": "", "url_gambar": ""})

        with state.lock:
            plate = state.take_plate()
            url = state.capture(plate)

        log.info("[%s] read %s", state.name, plate)
        return _json({"plate_num": plate, "url_gambar": url})

    @app.get("/image/{image_id}.jpg")
    def image(image_id: str) -> Response:
        with state.lock:
            data = state.images.get(image_id)
        if data is None:
            raise HTTPException(status_code=404, detail="snapshot expired")
        return Response(content=data, media_type="image/jpeg")

    # -- simulator control -------------------------------------------------

    @app.get("/mock/state")
    def mock_state() -> dict:
        with state.lock:
            return {
                "name": state.name,
                "gate": state.gate,
                "mode": state.mode,
                "queued": list(state.queued),
                "last_plate": state.last_plate,
                "reads": state.reads,
                "signage": state.signage,
                "images_held": len(state.images),
            }

    @app.post("/mock/queue")
    def queue_plate(body: dict) -> dict:
        plate = str(body.get("plate", "")).upper().replace(" ", "")
        if not plate:
            raise HTTPException(status_code=400, detail="plate is empty")
        with state.lock:
            state.queued.append(plate)
            depth = len(state.queued)
        log.info("[%s] queued %s", state.name, plate)
        return {"queued": plate, "depth": depth}

    @app.post("/mock/mode")
    def set_mode(body: dict) -> dict:
        mode = str(body.get("mode", ""))
        if mode not in MODES:
            raise HTTPException(status_code=400, detail=f"mode must be one of {MODES}")
        with state.lock:
            state.mode = mode
        log.info("[%s] mode -> %s", state.name, mode)
        return {"mode": mode}

    @app.post("/mock/reset")
    def reset() -> dict:
        with state.lock:
            state.mode = MODE_OK
            state.queued.clear()
        return {"status": "reset"}

    return app


def _json(payload: dict) -> Response:
    """Serialise verbatim so empty strings survive into the response."""
    return Response(content=json.dumps(payload), media_type="application/json")


class LprMqtt:
    """The MQTT side of an LPR unit.

    Entry units listen for signage changes. Exit units also publish their plate
    reads, since nothing polls them over HTTP.
    """

    def __init__(self, bus: MqttBus, state: LprState, *, publishes_reads: bool) -> None:
        self.bus = bus
        self.state = state
        self.publishes_reads = publishes_reads

    def start(self) -> None:
        self.bus.subscribe_raw(gate_status_topic(self.state.gate), self._on_signage)
        if self.publishes_reads:
            self.bus.subscribe(sim_topic(self.state.gate), self._on_sim)
        self.bus.connect()
        log.info("[%s] MQTT connected", self.state.name)

    def stop(self) -> None:
        self.bus.disconnect()

    def _on_signage(self, _topic: str, payload: str) -> None:
        try:
            status = json.loads(payload).get("status", "?")
        except json.JSONDecodeError:
            status = payload
        self.state.signage = status
        log.info("[%s] signage: %s", self.state.name, status)

    def _on_sim(self, _topic: str, message: Envelope) -> None:
        """A vehicle drove past the exit camera."""
        if message.method != SIM_READ_PLATE:
            return

        with self.state.lock:
            requested = message.get("plate")
            if requested:
                self.state.queued.append(str(requested).upper().replace(" ", ""))
            if self.state.mode == MODE_NO_PLATE:
                plate, url = "", ""
            else:
                plate = self.state.take_plate()
                url = self.state.capture(plate)

        log.info("[%s] read %s, announcing on MQTT", self.state.name, plate or "(none)")
        self.bus.publish_raw(
            gate_out_pos_topic(self.state.gate),
            json.dumps({"plate_num": plate, "url_gambar": url}),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock LPR unit")
    parser.add_argument("--gate", required=True, help="gate number this unit watches")
    parser.add_argument(
        "--publishes-reads",
        action="store_true",
        help="behave like the exit unit: announce plate reads over MQTT",
    )
    parser.add_argument("--env", default=None)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)-10s | %(message)s",
    )

    config = load_config(args.env)
    device = config.lpr_for(args.gate)
    state = LprState(device.name, args.gate, device.public_url, args.seed)

    bus = MqttBus(config.broker, client_id=f"lpr-{args.gate}")
    mqtt_side = LprMqtt(bus, state, publishes_reads=args.publishes_reads)
    mqtt_side.start()

    if not device.serves_http:
        # Reproduces the live exit unit, which accepts no TCP connections.
        log.warning(
            "[%s] serves_http is false — no HTTP listener, as on site (§7.2)",
            device.name,
        )
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            mqtt_side.stop()
        return

    log.info("[%s] serving HTTP on 0.0.0.0:%s", device.name, device.port)
    try:
        uvicorn.run(create_app(state), host="0.0.0.0", port=device.port, log_level="warning")
    finally:
        mqtt_side.stop()


if __name__ == "__main__":
    main()
