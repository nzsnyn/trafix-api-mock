"""Cashier client — stands in for the Tauri desktop app at ``.2``.

Talks to the API over exactly the endpoints the real app uses
(``detailtransaction`` then ``gateoutKasir``), so this exercises the same path
that carries every exit on site today.

The frontend source is not in the repo (flow.md §2, open question 7), so this
is written from the captured request bodies rather than from its code.
"""

from __future__ import annotations

import argparse
import sys

import httpx

from trafix.config import load_config


def _die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def _post(base_url: str, path: str, data: dict, method: str = "POST") -> dict:
    try:
        response = httpx.request(
            method,
            f"{base_url}{path}",
            data=data,
            headers={"Origin": "tauri://localhost"},  # as the real app sends
            timeout=10,
        )
    except httpx.HTTPError as exc:
        _die(f"cannot reach the API at {base_url}: {exc}")

    try:
        return response.json()
    except ValueError:
        _die(f"{path} returned non-JSON ({response.status_code}): {response.text[:200]}")


def _money(amount) -> str:
    try:
        return f"Rp{int(amount):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(amount)


def cmd_lookup(args, config) -> None:
    """Price a ticket without settling it."""
    payload = _post(
        config.api.base_url,
        "/api/gateout/detailtransaction",
        {
            "transaction_code": args.ticket or "",
            "police_number": args.plate or "",
            "gate_out": args.gate,
            "admin_id": args.admin,
            "shift_id": args.shift,
        },
    )
    _show(payload)


def cmd_settle(args, config) -> None:
    """Take payment and release the vehicle."""
    payload = _post(
        config.api.base_url,
        "/api/gateout/detailtransaction",
        {
            "transaction_code": args.ticket or "",
            "police_number": args.plate or "",
            "gate_out": args.gate,
            "admin_id": args.admin,
            "shift_id": args.shift,
        },
    )
    if payload.get("status") != "success":
        _show(payload)
        _die("nothing to settle")

    data = payload.get("data", {})
    total = data.get("total", 0)
    print(f"ticket   : {data.get('transaction_code')}")
    print(f"plate    : {data.get('police_number') or '-'}")
    print(f"duration : {data.get('duration')}")
    print(f"amount   : {_money(total)}")
    if data.get("name"):
        print(f"member   : {data['name']}")

    if not args.yes and total:
        answer = input(f"take {_money(total)} and open the gate? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("cancelled")
            return

    settled = _post(
        config.api.base_url,
        "/api/gateout/gateoutKasir",
        {
            "transaction_code": data.get("transaction_code") or args.ticket or "",
            "police_number": args.plate or data.get("police_number") or "",
            "gate_out": args.gate,
            "admin_id": args.admin,
            "shift_id": args.shift,
            "discount_card": "",
            "total_discount": 0,
            "lost_ticket": "1" if args.lost else "",
        },
        method="PUT",
    )

    status = settled.get("status")
    if status == "success":
        settled_total = (settled.get("data") or {}).get("total", 0)
        print(f"\n✓ settled — {_money(settled_total)}, barrier released")
    elif status == "already_paid":
        print("\n⚠ this ticket has already been used")
    else:
        print(f"\n✗ refused: {status} — {settled.get('message') or ''}")


def cmd_lost(args, config) -> None:
    """Lost ticket: charge the flat penalty against the plate."""
    if not args.plate:
        _die("a lost ticket needs --plate")
    args.lost = True
    args.ticket = None
    cmd_settle(args, config)


def _show(payload: dict) -> None:
    status = payload.get("status")
    if status != "success":
        print(f"status : {status}")
        if payload.get("message"):
            print(f"message: {payload['message']}")
        return

    data = payload.get("data", {})
    kind = payload.get("transaction")
    print(f"ticket    : {data.get('transaction_code')}")
    print(f"type      : {kind}")
    if data.get("name"):
        print(f"member    : {data['name']}")
    print(f"plate     : {data.get('police_number') or '-'}")
    print(f"checked in: {data.get('time_checkin')}")
    print(f"now       : {data.get('time_checkout')}")
    print(f"duration  : {data.get('duration')}")
    print(f"amount    : {_money(data.get('total'))}")
    if data.get("breakdown"):
        print(f"breakdown : {data['breakdown']}")
    print(f"photo in  : {data.get('cam_in')}")
    print(f"photo out : {data.get('cam_out')}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trafix-cashier",
        description="Cashier desk client (stands in for the Tauri app)",
    )
    parser.add_argument("--env", default=None)
    parser.add_argument("--gate", default="2", help="exit gate number")
    parser.add_argument("--admin", type=int, default=1, help="admin_id")
    parser.add_argument("--shift", type=int, default=1, help="shift_id")
    sub = parser.add_subparsers(dest="command", required=True)

    lookup = sub.add_parser("lookup", help="price a ticket without settling")
    lookup.add_argument("--ticket", help="ticket code / QR contents")
    lookup.add_argument("--plate", help="plate number")
    lookup.set_defaults(func=cmd_lookup, lost=False)

    settle = sub.add_parser("settle", help="take payment and open the exit")
    settle.add_argument("--ticket", help="ticket code / QR contents")
    settle.add_argument("--plate", help="plate number")
    settle.add_argument("--lost", action="store_true", help="ticket was lost")
    settle.add_argument("-y", "--yes", action="store_true", help="do not confirm")
    settle.set_defaults(func=cmd_settle)

    lost = sub.add_parser("lost", help="lost ticket, charged against the plate")
    lost.add_argument("--plate", required=True)
    lost.add_argument("-y", "--yes", action="store_true")
    lost.set_defaults(func=cmd_lost)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.env)
    args.func(args, config)


if __name__ == "__main__":
    main()
