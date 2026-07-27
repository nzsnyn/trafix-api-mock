"""The single message envelope shared by every MQTT message.

Request/response over pub/sub only works if a reply can be tied back to the
message that caused it, which is what ``correlation_id`` is for: a command
published by the server carries the ``msg_id`` of the event that triggered it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------

DEFAULT_TOPIC_ROOT = "trafix"

# Every process is one environment, so the root is set once at startup by
# whoever loads the config. Two environments sharing a broker (the simulator
# and the test suite, say) then never see each other's traffic.
_topic_root = DEFAULT_TOPIC_ROOT


def set_topic_root(root: str) -> None:
    global _topic_root
    _topic_root = root.strip("/") or DEFAULT_TOPIC_ROOT


def topic_root() -> str:
    return _topic_root


def evt_topic(lane: str) -> str:
    """Terminal -> server."""
    return f"{_topic_root}/{lane}/evt"


def cmd_topic(lane: str) -> str:
    """Server -> terminal."""
    return f"{_topic_root}/{lane}/cmd"


def state_topic(lane: str) -> str:
    """Retained terminal availability, also used as the MQTT last will."""
    return f"{_topic_root}/{lane}/state"


def sim_topic(lane: str) -> str:
    """Simulator control -> mock terminal.

    Not part of the production contract. The CLI uses it to make the mock
    terminal act as if a driver had pressed its button, so the events the
    server sees still originate from the terminal.
    """
    return f"{_topic_root}/{lane}/sim"


# ---------------------------------------------------------------------------
# Event types (terminal -> server)
# ---------------------------------------------------------------------------

EVT_TICKET_REQUEST = "ticket_request"
EVT_TICKET_PRINTED = "ticket_printed"
EVT_CHECKOUT_REQUEST = "checkout_request"
EVT_PAYMENT_SETTLED = "payment_settled"
EVT_VEHICLE_PASSED = "vehicle_passed"
EVT_GATE_TIMEOUT = "gate_timeout"

# ---------------------------------------------------------------------------
# Command types (server -> terminal)
# ---------------------------------------------------------------------------

CMD_PRINT_TICKET = "print_ticket"
CMD_CHECKOUT_RESULT = "checkout_result"
CMD_OPEN_GATE = "open_gate"
CMD_SHOW_MESSAGE = "show_message"
CMD_REJECT = "reject"

# ---------------------------------------------------------------------------
# Simulator control types (CLI -> mock terminal). Not a production contract.
# ---------------------------------------------------------------------------

SIM_PRESS_BUTTON = "press_button"
SIM_SCAN_TICKET = "scan_ticket"
SIM_SET_BEHAVIOUR = "set_behaviour"


class EnvelopeError(ValueError):
    """Raised when an incoming payload is not a usable envelope."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Envelope:
    type: str
    device_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str | None = None
    ts: str = field(default_factory=now_iso)

    def to_json(self) -> str:
        return json.dumps(
            {
                "msg_id": self.msg_id,
                "correlation_id": self.correlation_id,
                "ts": self.ts,
                "device_id": self.device_id,
                "type": self.type,
                "payload": self.payload,
            },
            separators=(",", ":"),
        )

    def reply(
        self, type: str, device_id: str, payload: dict[str, Any] | None = None
    ) -> "Envelope":
        """Build a message that answers this one."""
        return Envelope(
            type=type,
            device_id=device_id,
            payload=payload or {},
            correlation_id=self.msg_id,
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


def parse(raw: bytes | str) -> Envelope:
    """Decode a wire payload, raising :class:`EnvelopeError` on anything odd."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise EnvelopeError(f"payload is not utf-8: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise EnvelopeError(f"payload is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise EnvelopeError("payload must be a JSON object")

    for required in ("type", "device_id"):
        if not data.get(required):
            raise EnvelopeError(f"payload missing {required!r}")

    payload = data.get("payload") or {}
    if not isinstance(payload, dict):
        raise EnvelopeError("payload field must be an object")

    return Envelope(
        type=str(data["type"]),
        device_id=str(data["device_id"]),
        payload=payload,
        msg_id=str(data.get("msg_id") or uuid.uuid4()),
        correlation_id=data.get("correlation_id"),
        ts=str(data.get("ts") or now_iso()),
    )
