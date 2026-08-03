"""The wire protocol spoken by the gate hardware.

Every topic name, envelope field and method string here was taken from the
live-site capture documented in ``flow.md`` §8, cross-checked against the
Laravel source at ``Trafix/mqtt_app_lpr``. Nothing in this module is invented —
if you change a constant here, real hardware stops understanding us.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Topics
#
# Note the inconsistency in the real system: the entry side uses an
# upper-case, leading-slash convention while the exit LPR publishes to a
# lower-case one. That is how the devices are configured on site (flow.md §8),
# so it is reproduced rather than tidied up.
# ---------------------------------------------------------------------------


def gate_event_topic(gate: str | int) -> str:
    """Gate controller -> server. Sensor inputs and command acknowledgements."""
    return f"/GATE/event/{gate}"


def gate_in_topic(gate: str | int) -> str:
    """Server -> gate controller. ``txUartData`` (print) and ``outputCtrl`` (relay)."""
    return f"/GATE/IN/{gate}"


def gate_status_topic(gate: str | int) -> str:
    """Server -> LPR unit. Drives the signage: ``welcome`` / ``thanks``."""
    return f"/GATE/IN/{gate}/status"


def gate_out_pos_topic(gate: str | int) -> str:
    """Exit LPR -> server. Announces a plate read at the exit."""
    return f"gate/out/{gate}/pos"


def gate_out_topic(gate: str | int) -> str:
    """Server -> exit gate controller.

    **This topic does not exist in production.** flow.md §7.6 records that
    nothing ever commands the exit barrier — `GateoutController` contains no
    MQTT publish at all. We add it, mirroring the entry convention, because a
    parking system whose exit barrier no software can open is not finished.
    Point a real exit controller at this topic, or change it to whatever the
    device already subscribes to.
    """
    return f"/GATE/OUT/{gate}"


# ---------------------------------------------------------------------------
# Methods carried in the envelope
# ---------------------------------------------------------------------------

METHOD_TX_UART_DATA = "txUartData"  # print bytes to the thermal printer
METHOD_OUTPUT_CTRL = "outputCtrl"  # actuate a relay and/or the beeper
METHOD_INPUT_INFO = "inputInfo"  # sensor state change (controller -> server)
METHOD_STATUS = "status"  # controller status report
METHOD_READ_CARD = "readCard"  # an RFID tag was presented to the card reader

# ---------------------------------------------------------------------------
# Sensor inputs on the gate controller.
#
# flow.md §5 marks this map INFERRED from observed state transitions, not from
# a datasheet. input1 was never seen active. Verify against the real hardware
# before trusting it for anything safety-related.
# ---------------------------------------------------------------------------

INPUT_ARRIVAL_LOOP = "input3"  # a vehicle is waiting at the gate
INPUT_TICKET_BUTTON = "input2"  # the driver pressed the ticket button
INPUT_PASS_LOOP = "input4"  # the vehicle has cleared the lane
INPUT_UNKNOWN = "input1"  # never observed active

# Signage states pushed to the LPR unit.
STATUS_WELCOME = "welcome"
STATUS_THANKS = "thanks"

# The default relay pulse: 1000 ms on relay1, with a 100 ms beep.
# Format is [state, duration_ms] (flow.md §8, marked INFERRED).
DEFAULT_BEEP = [1, 100]
DEFAULT_RELAY_PULSE = [1, 1000]

RELAY_BARRIER = "relay1Out"  # relay2/relay3 were never actuated on site


class ProtocolError(ValueError):
    """Raised when an incoming payload is not a usable envelope."""


@dataclass(frozen=True)
class Envelope:
    """The message envelope used on ``/GATE/IN/N`` and ``/GATE/event/N``.

    ``id`` is an md5 in the messages Laravel sends and a plain integer in some
    controller-originated ones, so it is kept as an opaque string.
    """

    method: str
    serial_no: str
    data: Any = field(default_factory=dict)
    id: str = ""
    version: str = "1.0"
    task_no: int = 2

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "serialNo": self.serial_no,
                "version": self.version,
                "taskNo": self.task_no,
                "method": self.method,
                "data": self.data,
            },
            separators=(",", ":"),
        )

    def get(self, key: str, default: Any = None) -> Any:
        if isinstance(self.data, dict):
            return self.data.get(key, default)
        return default


def parse(raw: bytes | str) -> Envelope:
    """Decode a wire payload into an :class:`Envelope`."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"payload is not utf-8: {exc}") from exc

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"payload is not valid JSON: {exc}") from exc

    if not isinstance(decoded, dict):
        raise ProtocolError("payload must be a JSON object")

    method = decoded.get("method")
    if not method:
        raise ProtocolError("payload has no 'method'")

    return Envelope(
        method=str(method),
        serial_no=str(decoded.get("serialNo", "")),
        data=decoded.get("data", {}),
        id=str(decoded.get("id", "")),
        version=str(decoded.get("version", "1.0")),
        task_no=int(decoded.get("taskNo", 2)),
    )


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------


def message_id(transaction_code: str, part: int) -> str:
    """Reproduce Laravel's ``md5($trxId . '-1')`` id scheme.

    See ``GateController::gatein()`` — the two ticket halves are identified by
    ``md5("<trx>-1")`` and ``md5("<trx>-2")``.
    """
    return hashlib.md5(f"{transaction_code}-{part}".encode()).hexdigest()


def print_ticket(serial_no: str, uart_blocks: list[dict], message_id: str = "") -> Envelope:
    """A ``txUartData`` command carrying ESC/POS blocks to the printer."""
    return Envelope(
        method=METHOD_TX_UART_DATA,
        serial_no=serial_no,
        data=uart_blocks,
        id=message_id,
        task_no=2,
    )


def open_barrier(
    serial_no: str,
    *,
    relay: str = RELAY_BARRIER,
    pulse_ms: int = 1000,
    beep_ms: int = 100,
) -> Envelope:
    """The command that physically raises the barrier.

    Mirrors the only relay command seen in 43 minutes of capture:
    ``{"beepOut":[1,100],"relay1Out":[1,1000]}``.
    """
    data: dict[str, Any] = {relay: [1, pulse_ms]}
    if beep_ms:
        data["beepOut"] = [1, beep_ms]
    return Envelope(
        method=METHOD_OUTPUT_CTRL, serial_no=serial_no, data=data, task_no=1
    )


def signage(status: str) -> str:
    """Payload for ``/GATE/IN/N/status``.

    Not an envelope — the capture shows a bare ``{"status":"welcome"}``.
    """
    return json.dumps({"status": status}, separators=(",", ":"))


def input_info(serial_no: str, **inputs: int) -> Envelope:
    """A sensor state report, as the gate controller sends it."""
    return Envelope(
        method=METHOD_INPUT_INFO, serial_no=serial_no, data=dict(inputs), task_no=2
    )


def controller_status(
    serial_no: str,
    *,
    inputs: dict[str, int] | None = None,
    relays: dict[str, int] | None = None,
    beep: int = 0,
) -> Envelope:
    """The periodic ``status`` heartbeat the real board publishes.

    Observed on site 2026-08-04 (roughly every 50 s) as
    ``{"method":"status","data":{"input1":0,...,"input4":0,"relay1":0,
    "relay2":0,"relay3":0,"beep":0}}``. Note the status side uses the short
    relay keys (``relay1``), unlike the command side's ``relay1Out``.
    """
    data: dict[str, Any] = {}
    for i in range(1, 5):
        data[f"input{i}"] = int((inputs or {}).get(f"input{i}", 0))
    for r in range(1, 4):
        data[f"relay{r}"] = int((relays or {}).get(f"relay{r}", 0))
    data["beep"] = int(beep)
    return Envelope(method=METHOD_STATUS, serial_no=serial_no, data=data, task_no=2)


def read_card(serial_no: str, card_no: str, *, reader: int = 1, card_len: int = 10) -> Envelope:
    """An RFID tag presented to the card reader.

    Observed on site 2026-08-04 as ``{"method":"readCard","data":{"reader":1,
    "cardLen":10,"cardNo":"006343040"}}``. ``cardLen`` is the reader's fixed
    buffer length (10) — not ``len(cardNo)``, which is 9 here. ``cardNo`` is a
    string: the leading zero is significant. No ack follows; it is a
    one-way event, not a command.
    """
    return Envelope(
        method=METHOD_READ_CARD,
        serial_no=serial_no,
        data={"reader": int(reader), "cardLen": int(card_len), "cardNo": str(card_no)},
        task_no=2,
    )


def ack(serial_no: str, method: str) -> Envelope:
    """The controller's acknowledgement of a command.

    The real board acks with **empty data**: observed on site 2026-08-04 as
    ``{"method":"txUartData","data":{}}`` and ``{"method":"outputCtrl","data":{}}``
    on ``/GATE/event/1``. This supersedes the earlier ``{"code":"0"}`` shape
    documented in flow.md §5/§8, which the board's current firmware does not send.
    """
    return Envelope(method=method, serial_no=serial_no, data={})
