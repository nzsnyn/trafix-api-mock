"""Read-only MQTT observer for the live site broker.

Subscribes to the broker and records every message. It **never publishes** — a
subscriber is the safest possible client on a shared broker:

* ``MqttBus`` appends a random suffix to the client id and uses a clean
  session with no will (``trafix/mqtt_bus.py``), so this cannot evict
  ``payment-subscriber-v3`` or any other production client (flow.md §8).
* No retained messages are written, so nothing on the broker outlives the run.

Every message is written to the ``--out`` file as one JSON line (for diffing
against ``trafix/protocol.py``) and rendered human-readable on the console.

Usage::

    .venv/bin/python -m scripts.observe --env site --timeout 300
    .venv/bin/python -m scripts.observe --env site --topic '/GATE/event/#' --stats

Fast topic map with mosquitto-clients (needs the real broker password, see
``mqtt_app_lpr/.env`` if the config default is wrong)::

    mosquitto_sub -h 192.168.1.1 -p 1883 -u bssparking -P '<pass>' \\
        -i trafix-observe-$$ -t '#' -v

Do not run ``trafix status --env site`` for this: it opens the production
database and GETs ``/mock/state`` at the real LPRs (cli/trafix.py:223).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trafix.config import load_config
from trafix.mqtt_bus import MqttBus
from trafix.protocol import ProtocolError, parse

log = logging.getLogger("observe")


def _render(payload: str) -> str:
    """Short human form: ``method=... serial=... data=...`` or raw payload."""
    try:
        envelope = parse(payload)
    except ProtocolError:
        return payload
    data = json.dumps(envelope.data, separators=(",", ":")) if envelope.data else ""
    parts = [f"method={envelope.method}"]
    if envelope.serial_no:
        parts.append(f"serial={envelope.serial_no}")
    if envelope.id:
        parts.append(f"id={envelope.id}")
    if data:
        parts.append(f"data={data}")
    return " ".join(parts)


def _as_record(topic: str, payload: str) -> dict[str, Any]:
    """The JSONL record for ``--out``; envelope parsed when possible."""
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "topic": topic,
        "payload": payload,
    }
    try:
        envelope = parse(payload)
    except ProtocolError:
        return record
    record["envelope"] = {
        "method": envelope.method,
        "serial_no": envelope.serial_no,
        "data": envelope.data,
        "id": envelope.id,
        "version": envelope.version,
        "task_no": envelope.task_no,
    }
    return record


class Observer:
    def __init__(self, bus: MqttBus, topics: list[str], out: Path) -> None:
        self.bus = bus
        self.topics = topics
        self.out = out
        self.counts: dict[str, int] = {}

    def start(self) -> None:
        self.out.parent.mkdir(parents=True, exist_ok=True)
        for topic in self.topics:
            self.bus.subscribe_raw(topic, self._on_message)
        self.bus.connect()
        log.info(
            "observing %s topic(s) on %s:%s as %s — read-only",
            len(self.topics),
            self.bus.broker.host,
            self.bus.broker.port,
            self.bus.client_id,
        )
        log.info("writing JSONL to %s (Ctrl-C to stop)", self.out)

    def stop(self) -> None:
        self.bus.disconnect()

    def _on_message(self, topic: str, payload: str) -> None:
        self.counts[topic] = self.counts.get(topic, 0) + 1
        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"{stamp}  {topic}  {_render(payload)}", flush=True)
        with self.out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_as_record(topic, payload)) + "\n")

    def report(self) -> None:
        if not self.counts:
            print("\nno messages observed")
            return
        print("\nper-topic counts:")
        for topic, count in sorted(self.counts.items(), key=lambda item: -item[1]):
            print(f"  {count:>6}  {topic}")
        print(f"\n  total: {sum(self.counts.values())} messages")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__.split("Usage:")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__[__doc__.index("Usage:"):],
    )
    parser.add_argument("--env", default="site", help="config environment (default: site)")
    parser.add_argument(
        "--topic",
        action="append",
        default=["#"],
        help="subscribe to this topic filter; repeatable (default: '#')",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="stop after this many seconds; 0 = run until Ctrl-C (default: 0)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("logs/observe.log"),
        help="JSONL capture file (default: logs/observe.log)",
    )
    parser.add_argument("--stats", action="store_true", help="print per-topic counts at exit")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )

    config = load_config(args.env)
    bus = MqttBus(config.broker, client_id="observe")
    observer = Observer(bus, topics=args.topic, out=args.out)

    try:
        observer.start()
    except (ConnectionError, OSError) as exc:
        log.error("cannot observe broker %s:%s: %s", config.broker.host, config.broker.port, exc)
        sys.exit(1)

    try:
        if args.timeout > 0:
            log.info("capturing for %.0fs", args.timeout)
            time.sleep(args.timeout)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        observer.stop()
        if args.stats:
            observer.report()


if __name__ == "__main__":
    main()
