"""Mock gate-open daemon — the cashier's local :8090 service.

In production the Tauri cashier app (on ``192.168.1.2``) settles an exit, then
POSTs ``police_number``/``lpr_plate``/``transaction_code`` to its own
``http://192.168.1.2:8090/open-gate`` (form-encoded). That daemon — no source
available, captured on the wire — is what physically raises the exit barrier.

This stands in for it: it accepts the same form POST, logs the fields, and
publishes the ``outputCtrl`` to ``/GATE/OUT/{gate}`` so the mock exit
controller opens — the simulator's version of the barrier physically rising.
"""

from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from trafix.config import load_config
from trafix.mqtt_bus import MqttBus
from trafix.protocol import gate_out_topic, open_barrier

log = logging.getLogger("open_gate")


def create_app(config, gate: str) -> FastAPI:
    controller = config.controller_for(gate)
    bus = MqttBus(config.broker, client_id=f"open-gate-{gate}")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        bus.connect()
        log.info("[gate %s] open-gate daemon connected to broker", gate)
        yield
        bus.disconnect()

    app = FastAPI(
        title=f"mock gate-open daemon (gate {gate})",
        lifespan=lifespan,
    )
    # The cashier page is served from another origin (:8000), so the browser
    # must be allowed to read the response — otherwise the fetch works but
    # reading it throws and the desk shows a misleading "gagal" warning.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["POST"],
        allow_headers=["*"],
    )

    @app.post("/open-gate")
    async def open_gate(request: Request) -> JSONResponse:
        form = await request.form()
        log.info(
            "[gate %s] open-gate: police_number=%r lpr_plate=%r transaction_code=%r",
            gate,
            form.get("police_number"),
            form.get("lpr_plate"),
            form.get("transaction_code"),
        )
        bus.publish(
            gate_out_topic(gate),
            open_barrier(
                controller.serial_no,
                pulse_ms=config.policies.barrier_pulse_ms,
                beep_ms=config.policies.barrier_beep_ms,
            ),
        )
        return JSONResponse({"status": "ok", "gate": gate})


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock cashier gate-open daemon")
    parser.add_argument("--gate", default="2", help="exit lane gate (default 2)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--env", default=None)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)-10s | %(message)s",
    )

    config = load_config(args.env)
    log.info(
        "[gate %s] open-gate daemon listening on %s:%s",
        args.gate,
        args.host,
        args.port,
    )
    uvicorn.run(
        create_app(config, args.gate),
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
