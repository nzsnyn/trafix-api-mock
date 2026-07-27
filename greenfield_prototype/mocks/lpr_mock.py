"""Mock LPR camera.

Serves the same HTTP contract the real camera will: ``POST /api/v1/capture``
returns a plate plus a URL to the snapshot, and that URL really resolves.

It also exposes a ``/mock/*`` control surface the real camera will not have.
That is how the CLI makes the exit camera read the same plate the entry camera
did, and how the error-policy tests force a timeout or an unreadable plate.
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
from pydantic import BaseModel, Field

from mocks.plate_image import random_plate, render_plate_jpeg
from trafix.config import load_config
from trafix.tickets import normalize_plate

log = logging.getLogger("lpr_mock")

# How the camera should behave on the next capture.
MODE_OK = "ok"
MODE_NO_PLATE = "no_plate"
MODE_LOW_CONFIDENCE = "low_confidence"
MODE_ERROR = "error"
MODE_TIMEOUT = "timeout"
MODES = (MODE_OK, MODE_NO_PLATE, MODE_LOW_CONFIDENCE, MODE_ERROR, MODE_TIMEOUT)

IMAGE_RETENTION = 200


class CaptureRequest(BaseModel):
    trigger: str = "gate"
    lane: str | None = None


class ModeRequest(BaseModel):
    mode: str = Field(description=f"one of {MODES}")
    sticky: bool = Field(
        default=True, description="if false, applies to the next capture only"
    )


class QueueRequest(BaseModel):
    plate: str
    confidence: float | None = None


class CameraState:
    """Everything the mock camera remembers, guarded by one lock."""

    def __init__(self, device: str, lane: str, public_url: str, seed: int | None) -> None:
        self.device = device
        self.lane = lane
        self.public_url = public_url.rstrip("/")
        self.rng = random.Random(seed)
        self.lock = threading.Lock()

        self.mode = MODE_OK
        self.sticky = True
        self.queued: deque[tuple[str, float | None]] = deque()
        self.images: dict[str, bytes] = {}
        self.image_order: deque[str] = deque()
        self.last_plate: str | None = None
        self.captures = 0

    def store_image(self, data: bytes) -> str:
        image_id = uuid.uuid4().hex[:16]
        self.images[image_id] = data
        self.image_order.append(image_id)
        while len(self.image_order) > IMAGE_RETENTION:
            self.images.pop(self.image_order.popleft(), None)
        return image_id

    def take_mode(self) -> str:
        mode = self.mode
        if not self.sticky:
            self.mode = MODE_OK
            self.sticky = True
        return mode

    def take_plate(self) -> tuple[str, float | None]:
        if self.queued:
            return self.queued.popleft()
        return random_plate(self.rng), None


def create_app(state: CameraState) -> FastAPI:
    app = FastAPI(title=f"Mock LPR {state.device}", version="1.0")

    # -- the contract the real camera must also implement ------------------

    @app.get("/api/v1/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "device": state.device,
            "lane": state.lane,
            "captures": state.captures,
        }

    @app.post("/api/v1/capture")
    def capture(request: CaptureRequest) -> Response:
        with state.lock:
            mode = state.take_mode()
            state.captures += 1
            lane = request.lane or state.lane

        if mode == MODE_TIMEOUT:
            log.warning("[%s] simulating a hung camera", state.device)
            time.sleep(30)  # the client's timeout fires long before this returns

        if mode == MODE_ERROR:
            log.warning("[%s] simulating a camera fault", state.device)
            raise HTTPException(status_code=503, detail="camera unavailable")

        now = datetime.now(timezone.utc)

        if mode == MODE_NO_PLATE:
            log.warning("[%s] simulating an unreadable plate", state.device)
            return _json(
                {
                    "plate": None,
                    "confidence": 0.0,
                    "image_url": None,
                    "captured_at": now.isoformat(timespec="milliseconds"),
                    "message": "no plate detected",
                }
            )

        with state.lock:
            plate, forced_confidence = state.take_plate()
            plate = normalize_plate(plate) or random_plate(state.rng)
            if mode == MODE_LOW_CONFIDENCE:
                confidence = round(state.rng.uniform(0.20, 0.50), 2)
            elif forced_confidence is not None:
                confidence = round(float(forced_confidence), 2)
            else:
                confidence = round(state.rng.uniform(0.82, 0.99), 2)

            image = render_plate_jpeg(
                plate,
                lane=lane,
                device=state.device,
                captured_at=now,
                confidence=confidence,
            )
            image_id = state.store_image(image)
            state.last_plate = plate

        log.info("[%s] captured %s (conf %.2f)", state.device, plate, confidence)
        return _json(
            {
                "plate": plate,
                "confidence": confidence,
                "image_url": f"{state.public_url}/images/{image_id}.jpg",
                "captured_at": now.isoformat(timespec="milliseconds"),
                "device": state.device,
                "lane": lane,
                "trigger": request.trigger,
            }
        )

    @app.get("/images/{image_id}.jpg")
    def image(image_id: str) -> Response:
        with state.lock:
            data = state.images.get(image_id)
        if data is None:
            raise HTTPException(status_code=404, detail="snapshot expired")
        return Response(content=data, media_type="image/jpeg")

    # -- simulator control, not part of the real camera --------------------

    @app.get("/mock/state")
    def mock_state() -> dict[str, object]:
        with state.lock:
            return {
                "device": state.device,
                "lane": state.lane,
                "mode": state.mode,
                "sticky": state.sticky,
                "queued": [plate for plate, _ in state.queued],
                "last_plate": state.last_plate,
                "captures": state.captures,
                "images_held": len(state.images),
            }

    @app.post("/mock/mode")
    def set_mode(request: ModeRequest) -> dict[str, object]:
        if request.mode not in MODES:
            raise HTTPException(status_code=400, detail=f"mode must be one of {MODES}")
        with state.lock:
            state.mode = request.mode
            state.sticky = request.sticky
        log.info("[%s] mode -> %s (sticky=%s)", state.device, request.mode, request.sticky)
        return {"mode": request.mode, "sticky": request.sticky}

    @app.post("/mock/queue")
    def queue_plate(request: QueueRequest) -> dict[str, object]:
        """Force the plate the next capture will return.

        This is what makes an exit read match its entry read.
        """
        plate = normalize_plate(request.plate)
        if not plate:
            raise HTTPException(status_code=400, detail="plate is empty")
        with state.lock:
            state.queued.append((plate, request.confidence))
            depth = len(state.queued)
        log.info("[%s] queued plate %s", state.device, plate)
        return {"queued": plate, "depth": depth}

    @app.post("/mock/reset")
    def reset() -> dict[str, object]:
        with state.lock:
            state.mode = MODE_OK
            state.sticky = True
            state.queued.clear()
        return {"status": "reset"}

    return app


def _json(payload: dict[str, object]) -> Response:
    """Return the body verbatim.

    FastAPI's own serialiser would drop a null plate from the response; the
    server needs to see that field explicitly to know the read failed.
    """
    return Response(content=json.dumps(payload), media_type="application/json")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock LPR camera")
    parser.add_argument(
        "--device", required=True, choices=["lpr_in", "lpr_out"], help="which camera"
    )
    parser.add_argument("--env", default=None, help="config environment override")
    parser.add_argument("--host", default=None, help="bind address override")
    parser.add_argument("--port", type=int, default=None, help="bind port override")
    parser.add_argument("--seed", type=int, default=None, help="fix the plate RNG")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    config = load_config(args.env)
    lane = "in" if args.device == "lpr_in" else "out"
    device_config = config.lpr_for(lane)

    state = CameraState(
        device=args.device,
        lane=lane,
        public_url=device_config.public_url,
        seed=args.seed,
    )

    host = args.host or "0.0.0.0"
    port = args.port or device_config.port
    log.info(
        "mock %s listening on %s:%s, snapshots served from %s",
        args.device,
        host,
        port,
        device_config.public_url,
    )
    uvicorn.run(create_app(state), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
