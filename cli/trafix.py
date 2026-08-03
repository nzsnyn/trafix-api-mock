"""Drive and inspect the simulator.

Nothing here is part of the production system. It exists so a human can make a
vehicle arrive, press the ticket button, force an LPR failure, and read back
what the system decided.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime

import httpx
from sqlalchemy import select

from trafix import db
from trafix.config import Config, ConfigError, load_config
from trafix.models import GateEvents, Transactions
from trafix.mqtt_bus import MqttBus
from trafix.protocol import (
    Envelope,
    gate_event_topic,
    gate_in_topic,
    gate_out_pos_topic,
    gate_out_topic,
    gate_status_topic,
    open_barrier,
)

# Mirrors the constants in the mocks; kept here so the CLI does not import them.
SIM_ARRIVE = "arrive"
SIM_PRESS = "press"
SIM_PASS = "pass"
SIM_CYCLE = "cycle"
SIM_CARD = "card"
SIM_SET = "set"
SIM_READ_PLATE = "read_plate"

# Relay keys the gate controller understands (flow.md §5: relay1 = entry
# barrier; relay2/relay3 were never actuated on site).
GATE_RELAYS = ("relay1Out", "relay2Out", "relay3Out")


def controller_sim_topic(gate: str) -> str:
    return f"trafix/sim/controller/{gate}"


def lpr_sim_topic(gate: str) -> str:
    return f"trafix/sim/lpr/{gate}"


def _die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _bus(config: Config, client_id: str) -> MqttBus:
    bus = MqttBus(config.broker, client_id=client_id)
    try:
        bus.connect(timeout=5.0)
    except (ConnectionError, OSError) as exc:
        _die(f"{exc}\nIs the broker running?  docker compose up -d mosquitto")
    return bus


def _send(config: Config, topic: str, method: str, **data) -> None:
    bus = _bus(config, "cli")
    bus.publish(topic, Envelope(method=method, serial_no="cli", data=data))
    time.sleep(0.3)  # let the publish flush before the process exits
    bus.disconnect()


def _open_db(config: Config) -> None:
    try:
        db.init_engine(config.database_url)
    except Exception as exc:
        _die(f"cannot open the database: {exc}")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_arrive(args, config) -> None:
    """A vehicle rolls onto the arrival loop."""
    _send(config, controller_sim_topic(args.gate), SIM_ARRIVE)
    print(f"gate {args.gate}: vehicle arrived")


def cmd_press(args, config) -> None:
    """The driver presses the ticket button."""
    _send(config, controller_sim_topic(args.gate), SIM_PRESS)
    print(f"gate {args.gate}: ticket button pressed")


def cmd_pass(args, config) -> None:
    """The vehicle clears the lane."""
    _send(config, controller_sim_topic(args.gate), SIM_PASS)
    print(f"gate {args.gate}: vehicle cleared")


def cmd_enter(args, config) -> None:
    """The whole entry sequence: arrive, press, and drive through."""
    if args.plate:
        _queue_plate(config, args.gate, args.plate)
    _send(config, controller_sim_topic(args.gate), SIM_CYCLE)
    print(f"gate {args.gate}: entry cycle started" + (f" as {args.plate}" if args.plate else ""))


def cmd_exit_read(args, config) -> None:
    """The exit LPR spots a plate and announces it."""
    _send(config, lpr_sim_topic(args.gate), SIM_READ_PLATE, plate=args.plate or "")
    print(f"gate {args.gate}: exit camera read {args.plate or '(random)'}")


def cmd_card(args, config) -> None:
    """A member taps an RFID card at the entry gate."""
    _send(config, controller_sim_topic(args.gate), SIM_CARD, card_no=args.card)
    print(f"gate {args.gate}: RFID card {args.card} presented")


def _relay_key(name: str) -> str:
    """Normalise ``relay2`` to the wire key ``relay2Out``, rejecting typos."""
    if name in GATE_RELAYS:
        return name
    if re.fullmatch(r"relay[1-3]", name):
        return f"{name}Out"
    _die(f"relay must be one of {', '.join(GATE_RELAYS)}")


def cmd_gate(args, config) -> None:
    """Pulse a relay on a gate controller (outputCtrl)."""
    relay = _relay_key(args.relay)
    try:
        serial = config.controller_for(args.gate).serial_no
    except ConfigError:
        serial = ""
        print(
            f"warning: no controller configured for gate {args.gate} "
            "(publishing with an empty serialNo)",
            file=sys.stderr,
        )

    topic = gate_out_topic(args.gate) if args.exit_lane else gate_in_topic(args.gate)
    envelope = open_barrier(
        serial, relay=relay, pulse_ms=args.ms, beep_ms=args.beep_ms
    )
    bus = _bus(config, "cli-gate")
    bus.publish(topic, envelope)
    time.sleep(0.3)
    bus.disconnect()

    print(f"{relay} pulse {args.ms} ms -> {topic}")
    print(f"serialNo : {serial or '(none)'}")
    print(envelope.to_json())


def _queue_plate(config: Config, gate: str, plate: str) -> None:
    lpr = config.lpr_for(gate)
    if not lpr.serves_http:
        return
    try:
        httpx.post(f"{lpr.base_url}/mock/queue", json={"plate": plate}, timeout=5)
    except httpx.HTTPError as exc:
        _die(f"cannot reach {lpr.name} at {lpr.base_url}: {exc}")


def cmd_lpr(args, config) -> None:
    """Drive a mock LPR unit: force its next read, or make it fail."""
    lpr = config.lpr_for(args.gate)
    if not lpr.serves_http:
        _die(f"{lpr.name} serves no HTTP in this environment (reproducing §7.2)")
    try:
        if args.queue:
            response = httpx.post(
                f"{lpr.base_url}/mock/queue", json={"plate": args.queue}, timeout=5
            )
        elif args.mode:
            response = httpx.post(
                f"{lpr.base_url}/mock/mode", json={"mode": args.mode}, timeout=5
            )
        elif args.reset:
            response = httpx.post(f"{lpr.base_url}/mock/reset", timeout=5)
        else:
            response = httpx.get(f"{lpr.base_url}/mock/state", timeout=5)
    except httpx.HTTPError as exc:
        _die(f"cannot reach {lpr.name} at {lpr.base_url}: {exc}")
    print(json.dumps(response.json(), indent=2))


def cmd_status(_args, config) -> None:
    """Is every component reachable?"""
    print(f"environment : {config.env}")
    print(f"database    : {_redact(config.database_url)}")
    print(f"broker      : {config.broker.host}:{config.broker.port}", end="  ")
    try:
        bus = MqttBus(config.broker, client_id="cli-status")
        bus.connect(timeout=3.0)
        bus.disconnect()
        print("[reachable]")
    except (ConnectionError, OSError) as exc:
        print(f"[UNREACHABLE: {type(exc).__name__}]")

    print(f"api         : {config.api.base_url}", end="  ")
    try:
        response = httpx.get(f"{config.api.base_url}/api/health", timeout=3)
        print(f"[{response.json().get('status')}]")
    except httpx.HTTPError as exc:
        print(f"[UNREACHABLE: {type(exc).__name__}]")

    for gate, lpr in sorted(config.lpr.items()):
        print(f"{lpr.name:<12}: {lpr.base_url}", end="  ")
        if not lpr.serves_http:
            print("[no HTTP by design — reproduces §7.2]")
            continue
        try:
            httpx.get(f"{lpr.base_url}/mock/state", timeout=3)
            print("[ok]")
        except httpx.HTTPError as exc:
            print(f"[UNREACHABLE: {type(exc).__name__}]")

    for gate, controller in sorted(config.controllers.items()):
        print(f"{controller.name:<12}: gate {gate}, serial {controller.serial_no}")

    _open_db(config)
    with db.session_scope() as session:
        total = session.scalar(select(Transactions).limit(1))
        inside = len(
            session.scalars(
                select(Transactions).where(Transactions.time_checkout.is_(None))
            ).all()
        )
        count = len(session.scalars(select(Transactions)).all())
    print(f"\ntransactions: {count} total, {inside} still inside")


def cmd_txn_list(args, config) -> None:
    _open_db(config)
    with db.session_scope() as session:
        query = select(Transactions).order_by(Transactions.transaction_id.desc())
        if args.inside:
            query = query.where(Transactions.time_checkout.is_(None))
        rows = session.scalars(query.limit(args.limit)).all()

    if not rows:
        print("no transactions")
        return

    header = (
        f"{'TICKET':<12} {'PLATE':<10} {'IN':<19} {'OUT':<19} "
        f"{'DURATION':<16} {'TOTAL':>10}  PAID"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row.transaction_code:<12} {(row.police_number or '-'):<10} "
            f"{row.time_checkin:<19} {(row.time_checkout or '-'):<19} "
            f"{(row.duration or '-'):<16} {_money(row.total):>10}  "
            f"{'yes' if row.payment_status == 'lunas' else 'no'}"
        )


def cmd_txn_show(args, config) -> None:
    _open_db(config)
    with db.session_scope() as session:
        row = session.scalar(
            select(Transactions).where(
                (Transactions.transaction_code == args.ident)
                | (Transactions.qrcode == args.ident)
                | (Transactions.police_number == args.ident.upper())
            )
        )
        if row is None:
            _die(f"no transaction matching {args.ident!r}")

        print(f"ticket        : {row.transaction_code}")
        print(f"qr code       : {row.qrcode}")
        print(f"plate         : {row.police_number or '-'}")
        print(f"vehicle_id    : {row.vehicle_id}")
        print(f"status        : {row.status} / gate_status={row.gate_status}")
        print(f"payment       : {row.payment_status or '-'} ({row.payment_type})")
        print(f"checked in    : {row.time_checkin}  gate {row.gate_in}")
        print(f"checked out   : {row.time_checkout or '-'}  gate {row.gate_out or '-'}")
        print(f"duration      : {row.duration or '-'}")
        print(f"total         : {_money(row.total)}")
        print(f"photo in      : {row.cam_in}")
        print(f"photo out     : {row.cam_out or '-'}")
        if row.keterangan:
            print(f"note          : {row.keterangan}")

        events = session.scalars(
            select(GateEvents)
            .where(GateEvents.transaction_code == row.transaction_code)
            .order_by(GateEvents.id)
        ).all()
        if events:
            print("\nevents:")
            for event in events:
                print(
                    f"  {event.ts:%Y-%m-%d %H:%M:%S}  {event.source:<6} "
                    f"{event.method:<22} {event.detail or ''}"
                )


def cmd_tail(_args, config) -> None:
    """Watch the gate traffic as it happens."""
    bus = _bus(config, "cli-tail")

    def show(topic: str, message: Envelope) -> None:
        data = json.dumps(message.data)
        if len(data) > 110:
            data = data[:107] + "..."
        print(f"{datetime.now():%H:%M:%S}  {topic:<22} {message.method:<12} {data}")

    def show_raw(topic: str, payload: str) -> None:
        print(f"{datetime.now():%H:%M:%S}  {topic:<22} {'':<12} {payload}")

    topics: list[str] = []
    for gate in sorted({*config.controllers, *config.lpr}):
        for topic in (
            gate_event_topic(gate),
            gate_in_topic(gate),
            gate_out_topic(gate),
        ):
            bus.subscribe(topic, show)
            topics.append(topic)
        for topic in (gate_status_topic(gate), gate_out_pos_topic(gate)):
            bus.subscribe_raw(topic, show_raw)
            topics.append(topic)

    print("watching:")
    for topic in topics:
        print(f"  {topic}")
    print("\nCtrl-C to stop\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bus.disconnect()


def _money(amount) -> str:
    if amount is None:
        return "-"
    try:
        return f"Rp{int(amount):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(amount)


def _redact(url: str) -> str:
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.partition("@")
    return f"{scheme}://***@{host}"


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trafix", description="Drive and inspect the Trafix simulator"
    )
    parser.add_argument("--env", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    enter = sub.add_parser("enter", help="full entry: arrive, press, drive through")
    enter.add_argument("--gate", default="1")
    enter.add_argument("--plate", help="force the plate the entry LPR will read")
    enter.set_defaults(func=cmd_enter)

    for name, func, help_text in (
        ("arrive", cmd_arrive, "vehicle rolls onto the arrival loop"),
        ("press", cmd_press, "driver presses the ticket button"),
        ("pass", cmd_pass, "vehicle clears the lane"),
    ):
        step = sub.add_parser(name, help=help_text)
        step.add_argument("--gate", default="1")
        step.set_defaults(func=func)

    exit_read = sub.add_parser("exit-read", help="exit camera reads a plate")
    exit_read.add_argument("--gate", default="2")
    exit_read.add_argument("--plate")
    exit_read.set_defaults(func=cmd_exit_read)

    card = sub.add_parser("card", help="member taps an RFID card at the entry")
    card.add_argument("--gate", default="1")
    card.add_argument("--card", default="006343040", help="RFID tag number (string)")
    card.set_defaults(func=cmd_card)

    gate = sub.add_parser("gate", help="pulse a relay on a gate controller")
    gate.add_argument("--gate", default="1")
    gate.add_argument(
        "--relay", default="relay1Out", help="relay to pulse: relay1 | relay2 | relay3"
    )
    gate.add_argument("--ms", type=int, default=1000, help="pulse duration in ms")
    gate.add_argument("--beep-ms", type=int, default=0, help="beeper ms (0 = silent)")
    gate.add_argument(
        "--exit-lane", action="store_true", help="send to /GATE/OUT/{gate} instead"
    )
    gate.set_defaults(func=cmd_gate)

    lpr = sub.add_parser("lpr", help="drive a mock LPR unit")
    lpr.add_argument("--gate", default="1")
    lpr.add_argument("--queue", metavar="PLATE", help="force the next read")
    lpr.add_argument(
        "--mode", choices=["ok", "no_plate", "error", "timeout"], help="make it misbehave"
    )
    lpr.add_argument("--reset", action="store_true")
    lpr.set_defaults(func=cmd_lpr)

    status = sub.add_parser("status", help="check every component")
    status.set_defaults(func=cmd_status)

    txn = sub.add_parser("txn", help="inspect transactions")
    txn_sub = txn.add_subparsers(dest="txn_command", required=True)

    txn_list = txn_sub.add_parser("list", help="recent transactions")
    txn_list.add_argument("--limit", type=int, default=20)
    txn_list.add_argument("--inside", action="store_true", help="only cars still inside")
    txn_list.set_defaults(func=cmd_txn_list)

    txn_show = txn_sub.add_parser("show", help="one transaction in full")
    txn_show.add_argument("ident", help="ticket code, QR code, or plate")
    txn_show.set_defaults(func=cmd_txn_show)

    tail = sub.add_parser("tail", help="watch gate traffic")
    tail.set_defaults(func=cmd_tail)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.env)
    args.func(args, config)


if __name__ == "__main__":
    main()
