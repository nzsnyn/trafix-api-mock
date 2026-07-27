"""Command line for driving and inspecting the simulator.

Nothing here is part of the production system. It exists so a human can press
the entry button, scan a ticket at the exit, force a camera fault, and read
back what the server decided.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

import httpx

from trafix.config import Config, load_config
from trafix.db import Database, Transaction
from trafix.envelope import (
    SIM_PRESS_BUTTON,
    SIM_SCAN_TICKET,
    SIM_SET_BEHAVIOUR,
    Envelope,
    cmd_topic,
    evt_topic,
    sim_topic,
    state_topic,
)
from trafix.mqtt_bus import MqttBus


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _bus(config: Config, client_id: str) -> MqttBus:
    bus = MqttBus(config.broker, client_id=client_id)
    try:
        bus.connect(timeout=5.0)
    except ConnectionError as exc:
        _die(
            f"{exc}\n"
            f"Is the broker running?  docker compose up -d mosquitto"
        )
    return bus


def _die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _send_sim(config: Config, lane: str, type: str, payload: dict) -> None:
    bus = _bus(config, f"cli-{type}")
    bus.publish(sim_topic(lane), Envelope(type=type, device_id="cli", payload=payload))
    time.sleep(0.3)  # let the publish flush before the process exits
    bus.disconnect()


def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


def _fmt_money(amount: int | None, currency: str) -> str:
    if amount is None:
        return "-"
    return f"{currency} {amount:,}".replace(",", ".")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_press(args: argparse.Namespace, config: Config) -> None:
    """Simulate a driver pressing the ticket button on the entry lane."""
    _send_sim(config, args.lane, SIM_PRESS_BUTTON, {})
    print(f"button pressed on lane {args.lane}")


def cmd_scan(args: argparse.Namespace, config: Config) -> None:
    """Simulate a ticket being scanned at the exit."""
    barcode = args.barcode or ""
    if not barcode and not args.lost:
        _die("give a --barcode, or use --lost for a lost ticket")
    _send_sim(
        config,
        args.lane,
        SIM_SCAN_TICKET,
        {"barcode": barcode, "lost_ticket": args.lost},
    )
    print(f"scanned {'LOST TICKET' if args.lost else barcode} on lane {args.lane}")


def cmd_behaviour(args: argparse.Namespace, config: Config) -> None:
    """Change what the mock terminal does by itself."""
    payload: dict[str, object] = {}
    for name in ("auto_print", "auto_pay", "auto_pass"):
        value = getattr(args, name)
        if value is not None:
            payload[name] = value == "on"
    for name in ("print_delay", "pay_delay", "pass_delay"):
        value = getattr(args, name)
        if value is not None:
            payload[name] = value
    if not payload:
        _die("nothing to change; see --help")
    _send_sim(config, args.lane, SIM_SET_BEHAVIOUR, payload)
    print(f"lane {args.lane} behaviour: {payload}")


def cmd_camera(args: argparse.Namespace, config: Config) -> None:
    """Drive a mock camera: force its next read, or make it fail."""
    lpr = config.lpr_for(args.lane)
    try:
        if args.queue:
            response = httpx.post(
                f"{lpr.base_url}/mock/queue",
                json={"plate": args.queue, "confidence": args.confidence},
                timeout=5,
            )
        elif args.mode:
            response = httpx.post(
                f"{lpr.base_url}/mock/mode",
                json={"mode": args.mode, "sticky": args.sticky},
                timeout=5,
            )
        elif args.reset:
            response = httpx.post(f"{lpr.base_url}/mock/reset", timeout=5)
        else:
            response = httpx.get(f"{lpr.base_url}/mock/state", timeout=5)
    except httpx.HTTPError as exc:
        _die(f"cannot reach {lpr.name} at {lpr.base_url}: {exc}")

    print(json.dumps(response.json(), indent=2))


def cmd_status(_args: argparse.Namespace, config: Config) -> None:
    """Show whether every piece of the system is reachable."""
    print(f"environment : {config.env}")
    print(f"database    : {config.database}")
    print(f"broker      : {config.broker.host}:{config.broker.port}", end="  ")
    try:
        bus = MqttBus(config.broker, client_id="cli-status")
        bus.connect(timeout=3.0)
        bus.disconnect()
        print("[reachable]")
    except (ConnectionError, OSError) as exc:
        print(f"[UNREACHABLE: {type(exc).__name__}]")

    for lane in ("in", "out"):
        lpr = config.lpr_for(lane)
        print(f"{lpr.name:<12}: {lpr.base_url}", end="  ")
        try:
            response = httpx.get(f"{lpr.base_url}/api/v1/health", timeout=3)
            body = response.json()
            print(f"[ok, {body.get('captures', 0)} captures]")
        except httpx.HTTPError as exc:
            print(f"[UNREACHABLE: {type(exc).__name__}]")

    db = Database(config.database)
    counts = db.count_by_status()
    db.close()
    print("\ntransactions:")
    if not counts:
        print("  (none yet)")
    for status, count in sorted(counts.items()):
        print(f"  {status:<18} {count}")


def cmd_txn_list(args: argparse.Namespace, config: Config) -> None:
    """List recent transactions."""
    db = Database(config.database)
    rows = db.list_transactions(status=args.status, limit=args.limit)
    db.close()

    if not rows:
        print("no transactions")
        return

    header = (
        f"{'TICKET':<20} {'STATUS':<17} {'PLATE IN':<10} {'PLATE OUT':<10} "
        f"{'MATCH':<6} {'MIN':>5} {'FEE':>12}  BARCODE"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        match = "-" if row.plate_match is None else ("yes" if row.plate_match else "NO")
        flag = " *" if row.flagged else ""
        print(
            f"{row.ticket_no:<20} {row.status + flag:<17} "
            f"{(row.entry_plate or '-'):<10} {(row.exit_plate or '-'):<10} "
            f"{match:<6} {(row.duration_minutes if row.duration_minutes is not None else '-')!s:>5} "
            f"{_fmt_money(row.fee, config.tariff.currency):>12}  {row.barcode}"
        )
    if any(row.flagged for row in rows):
        print("\n* flagged for operator review")


def cmd_txn_show(args: argparse.Namespace, config: Config) -> None:
    """Show one transaction and everything that happened to it."""
    db = Database(config.database)
    row = db.get_by_barcode(args.ident) or db.get_by_ticket(args.ident)
    if row is None:
        db.close()
        _die(f"no transaction with barcode or ticket {args.ident!r}")

    _print_transaction(row, config)
    print("\nevents:")
    for event in reversed(db.list_events(transaction_id=row.id, limit=100)):
        detail = f"  {json.dumps(event['detail'])}" if event["detail"] else ""
        print(f"  {_fmt_time(event['ts'])}  {event['source']:<9} {event['type']}{detail}")
    db.close()


def _print_transaction(row: Transaction, config: Config) -> None:
    currency = config.tariff.currency
    print(f"ticket      : {row.ticket_no}")
    print(f"barcode     : {row.barcode}")
    print(f"status      : {row.status}")
    print(f"entry       : {_fmt_time(row.entry_time)}  lane {row.entry_lane}")
    print(f"  plate     : {row.entry_plate or '-'}")
    print(f"  snapshot  : {row.entry_image_url or '-'}")
    print(f"exit        : {_fmt_time(row.exit_time)}  lane {row.exit_lane or '-'}")
    print(f"  plate     : {row.exit_plate or '-'}")
    print(f"  snapshot  : {row.exit_image_url or '-'}")
    print(f"plate match : {row.plate_match if row.plate_match is not None else '-'}")
    print(f"duration    : {row.duration_minutes if row.duration_minutes is not None else '-'} min")
    print(f"fee         : {_fmt_money(row.fee, currency)}  (paid: {row.paid})")
    if row.flagged:
        print(f"FLAGGED     : {row.flag_reason}")


def cmd_tail(args: argparse.Namespace, config: Config) -> None:
    """Watch MQTT traffic on both lanes as it happens."""
    bus = _bus(config, "cli-tail")

    def show(topic: str, message: Envelope) -> None:
        leg = topic.rsplit("/", 1)[-1]
        arrow = {"evt": "->", "cmd": "<-", "sim": " ~"}.get(leg, "  ")
        payload = json.dumps(message.payload) if message.payload else ""
        print(
            f"{datetime.now():%H:%M:%S}  {arrow} {topic:<18} "
            f"{message.type:<18} {payload}"
        )

    def show_raw(topic: str, payload: str) -> None:
        print(f"{datetime.now():%H:%M:%S}   = {topic:<18} {payload}")

    for lane in ("in", "out"):
        bus.subscribe(evt_topic(lane), show)
        bus.subscribe(cmd_topic(lane), show)
        bus.subscribe(sim_topic(lane), show)
        bus.subscribe_raw(state_topic(lane), show_raw)

    print("watching trafix/+/{evt,cmd,sim,state} — Ctrl-C to stop\n")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bus.disconnect()


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trafix", description="Drive and inspect the parking gate simulator"
    )
    parser.add_argument("--env", default=None, help="config environment override")
    sub = parser.add_subparsers(dest="command", required=True)

    press = sub.add_parser("press", help="press the ticket button")
    press.add_argument("--lane", default="in", choices=["in", "out"])
    press.set_defaults(func=cmd_press)

    scan = sub.add_parser("scan", help="scan a ticket at the exit")
    scan.add_argument("--lane", default="out", choices=["in", "out"])
    scan.add_argument("--barcode", default=None)
    scan.add_argument("--lost", action="store_true", help="declare a lost ticket")
    scan.set_defaults(func=cmd_scan)

    behaviour = sub.add_parser("behaviour", help="change mock terminal behaviour")
    behaviour.add_argument("--lane", required=True, choices=["in", "out"])
    for name in ("auto_print", "auto_pay", "auto_pass"):
        behaviour.add_argument(f"--{name.replace('_', '-')}", dest=name,
                               choices=["on", "off"], default=None)
    for name in ("print_delay", "pay_delay", "pass_delay"):
        behaviour.add_argument(f"--{name.replace('_', '-')}", dest=name,
                               type=float, default=None)
    behaviour.set_defaults(func=cmd_behaviour)

    camera = sub.add_parser("camera", help="drive a mock LPR camera")
    camera.add_argument("--lane", required=True, choices=["in", "out"])
    camera.add_argument("--queue", metavar="PLATE", help="force the next plate read")
    camera.add_argument("--confidence", type=float, default=None)
    camera.add_argument(
        "--mode",
        choices=["ok", "no_plate", "low_confidence", "error", "timeout"],
        help="make the camera misbehave",
    )
    camera.add_argument(
        "--sticky",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="keep the mode until changed (default), or apply it once",
    )
    camera.add_argument("--reset", action="store_true", help="back to normal")
    camera.set_defaults(func=cmd_camera)

    status = sub.add_parser("status", help="check every component is reachable")
    status.set_defaults(func=cmd_status)

    txn = sub.add_parser("txn", help="inspect transactions")
    txn_sub = txn.add_subparsers(dest="txn_command", required=True)

    txn_list = txn_sub.add_parser("list", help="list recent transactions")
    txn_list.add_argument("--status", default=None)
    txn_list.add_argument("--limit", type=int, default=20)
    txn_list.set_defaults(func=cmd_txn_list)

    txn_show = txn_sub.add_parser("show", help="show one transaction in full")
    txn_show.add_argument("ident", help="barcode or ticket number")
    txn_show.set_defaults(func=cmd_txn_show)

    tail = sub.add_parser("tail", help="watch MQTT traffic")
    tail.set_defaults(func=cmd_tail)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.env)
    args.func(args, config)


if __name__ == "__main__":
    main()
