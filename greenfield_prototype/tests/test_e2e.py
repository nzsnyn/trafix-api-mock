"""End-to-end: real broker, real HTTP, every process talking over the wire.

Skipped automatically when the broker is not running, so ``pytest`` stays
useful without Docker. Start it with::

    docker compose up -d mosquitto
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from trafix.config import load_config
from trafix.db import STATUS_ACTIVE, STATUS_CLOSED, STATUS_PENDING, Database
from trafix.envelope import (
    SIM_PRESS_BUTTON,
    SIM_SCAN_TICKET,
    SIM_SET_BEHAVIOUR,
    Envelope,
    sim_topic,
)
from trafix.mqtt_bus import MqttBus

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable

# The simulator runs on its own ports and database so a test never disturbs a
# running instance.
E2E_ENV = "e2e"


def broker_is_up(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def config():
    config = load_config(E2E_ENV)
    if not broker_is_up(config.broker.host, config.broker.port):
        pytest.skip(
            f"no MQTT broker at {config.broker.host}:{config.broker.port} "
            f"— run: docker compose up -d mosquitto"
        )
    return config


def _spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [PYTHON, "-m", *args],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for_http(url: str, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise TimeoutError(f"{url} never answered")


@pytest.fixture(scope="module")
def system(config):
    """Bring up both cameras, both terminals and the server."""
    if config.database.exists():
        config.database.unlink()

    processes = [
        _spawn(["mocks.lpr_mock", "--device", "lpr_in", "--env", E2E_ENV]),
        _spawn(["mocks.lpr_mock", "--device", "lpr_out", "--env", E2E_ENV]),
    ]
    try:
        for lane in ("in", "out"):
            _wait_for_http(f"{config.lpr_for(lane).base_url}/api/v1/health")

        processes += [
            _spawn(["mocks.manless_mock", "--lane", "in", "--env", E2E_ENV]),
            _spawn(["mocks.manless_mock", "--lane", "out", "--env", E2E_ENV]),
            _spawn(["trafix.server.app", "--env", E2E_ENV]),
        ]
        time.sleep(2.5)  # terminals announce themselves, server subscribes

        for process in processes:
            if process.poll() is not None:
                raise RuntimeError(
                    f"a component died on startup:\n{process.stdout.read()}"
                )

        yield config
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


@pytest.fixture
def bus(system):
    bus = MqttBus(system.broker, client_id="e2e")
    bus.connect(timeout=5)
    yield bus
    bus.disconnect()


@pytest.fixture
def db(system):
    database = Database(system.database)
    yield database
    database.close()


@pytest.fixture(autouse=True)
def settled(db):
    """Let the entry lane finish before the next test presses the button.

    The tests share one running system, and the button debounce keys on a
    PENDING ticket. A test that returns while its car is still on the loop
    would have the following test's press swallowed as a repeat press.
    """
    _wait_for_entry_lane_idle(db)
    yield
    _wait_for_entry_lane_idle(db)


def _wait_for_entry_lane_idle(db: Database, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        latest = newest(db)
        if latest is None or latest.status != STATUS_PENDING:
            return
        time.sleep(0.25)


def press_button(bus: MqttBus, lane: str = "in") -> None:
    bus.publish(
        sim_topic(lane), Envelope(type=SIM_PRESS_BUTTON, device_id="e2e", payload={})
    )


def scan_ticket(bus: MqttBus, barcode: str, lane: str = "out", lost: bool = False) -> None:
    bus.publish(
        sim_topic(lane),
        Envelope(
            type=SIM_SCAN_TICKET,
            device_id="e2e",
            payload={"barcode": barcode, "lost_ticket": lost},
        ),
    )


def set_behaviour(bus: MqttBus, lane: str, **flags) -> None:
    bus.publish(
        sim_topic(lane),
        Envelope(type=SIM_SET_BEHAVIOUR, device_id="e2e", payload=flags),
    )
    time.sleep(0.3)


def queue_plate(config, lane: str, plate: str) -> None:
    httpx.post(f"{config.lpr_for(lane).base_url}/mock/queue", json={"plate": plate},
               timeout=5).raise_for_status()


def set_camera_mode(config, lane: str, mode: str, sticky: bool = True) -> None:
    httpx.post(
        f"{config.lpr_for(lane).base_url}/mock/mode",
        json={"mode": mode, "sticky": sticky},
        timeout=5,
    ).raise_for_status()


def wait_for(predicate, timeout: float = 12.0, interval: float = 0.25):
    """Poll until ``predicate`` returns something truthy, or fail the test."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"condition never became true (last value: {last!r})")


def newest(db: Database):
    rows = db.list_transactions(limit=1)
    return rows[0] if rows else None


# -- the tests --------------------------------------------------------------


def test_check_in_creates_an_active_transaction(system, bus, db):
    queue_plate(system, "in", "B1111AAA")
    before = newest(db)
    press_button(bus)

    transaction = wait_for(
        lambda: (
            row
            if (row := newest(db))
            and (before is None or row.id != before.id)
            and row.status == STATUS_ACTIVE
            else None
        )
    )

    assert transaction.entry_plate == "B1111AAA"
    assert transaction.entry_lane == "in"
    assert transaction.barcode
    assert transaction.entry_image_url


def test_the_snapshot_url_actually_serves_an_image(system, bus, db):
    queue_plate(system, "in", "B2222BBB")
    press_button(bus)
    transaction = wait_for(
        lambda: (
            row if (row := db.find_active_by_plate("B2222BBB")) else None
        )
    )

    response = httpx.get(transaction.entry_image_url, timeout=5)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content.startswith(b"\xff\xd8")  # JPEG magic


def test_full_check_in_then_check_out(system, bus, db):
    plate = "B3333CCC"
    queue_plate(system, "in", plate)
    press_button(bus)
    entry = wait_for(lambda: db.find_active_by_plate(plate))

    # The exit camera must see the same car.
    queue_plate(system, "out", plate)
    scan_ticket(bus, entry.barcode)

    closed = wait_for(
        lambda: (
            row
            if (row := db.get(entry.id)) and row.status == STATUS_CLOSED
            else None
        )
    )

    assert closed.plate_match is True
    assert closed.exit_plate == plate
    assert closed.paid is True
    assert closed.fee == 0  # left within the grace period
    assert closed.duration_minutes == 0
    assert closed.exit_image_url


def test_plate_mismatch_is_flagged_but_still_lets_the_car_out(system, bus, db):
    queue_plate(system, "in", "B4444DDD")
    press_button(bus)
    entry = wait_for(lambda: db.find_active_by_plate("B4444DDD"))

    queue_plate(system, "out", "D9999ZZZ")  # a different car at the exit
    scan_ticket(bus, entry.barcode)

    closed = wait_for(
        lambda: (
            row if (row := db.get(entry.id)) and row.status == STATUS_CLOSED else None
        )
    )
    assert closed.plate_match is False
    assert closed.flagged is True
    assert closed.flag_reason == "plate_mismatch"


def test_unknown_barcode_never_opens_the_gate(system, bus, db):
    before = len(db.list_transactions(limit=500))
    scan_ticket(bus, "999999999999")
    time.sleep(2)
    assert len(db.list_transactions(limit=500)) == before, (
        "a bogus scan must not create a transaction"
    )


def test_entry_survives_a_dead_camera(system, bus, db):
    """Default policy is allow: the driver still gets a ticket."""
    set_camera_mode(system, "in", "error")
    try:
        before = newest(db)
        press_button(bus)
        transaction = wait_for(
            lambda: (
                row
                if (row := newest(db)) and (before is None or row.id != before.id)
                else None
            )
        )
        assert transaction.entry_plate == "UNKNOWN"
        assert transaction.flagged is True
    finally:
        set_camera_mode(system, "in", "ok")


def test_a_car_that_entered_unread_can_still_leave(system, bus, db):
    set_camera_mode(system, "in", "no_plate")
    try:
        before = newest(db)
        press_button(bus)
        entry = wait_for(
            lambda: (
                row
                if (row := newest(db))
                and (before is None or row.id != before.id)
                and row.status == STATUS_ACTIVE
                else None
            )
        )
    finally:
        set_camera_mode(system, "in", "ok")

    queue_plate(system, "out", "B5555EEE")
    scan_ticket(bus, entry.barcode)

    closed = wait_for(
        lambda: (
            row if (row := db.get(entry.id)) and row.status == STATUS_CLOSED else None
        )
    )
    assert closed.plate_match is None, "an unknown entry plate is not a mismatch"


def test_repeat_button_press_does_not_issue_two_tickets(system, bus, db):
    # Hold the car on the loop so the ticket stays PENDING for the whole test.
    # Debounce only applies to a ticket nobody has driven away with yet, so
    # letting the entry complete would race the second press.
    set_behaviour(bus, "in", auto_pass=False)
    try:
        queue_plate(system, "in", "B6666FFF")
        before = len(db.list_transactions(limit=500))
        press_button(bus)
        wait_for(lambda: len(db.list_transactions(limit=500)) > before)
        count_after_first = len(db.list_transactions(limit=500))

        press_button(bus)  # the driver presses again immediately
        time.sleep(2)

        assert len(db.list_transactions(limit=500)) == count_after_first
    finally:
        # Drain the car still sitting on the loop, so the lane is idle for
        # whatever runs next. The extra press is debounced into a reprint of
        # the same ticket, which is exactly the behaviour under test.
        set_behaviour(bus, "in", auto_pass=True)
        press_button(bus)
        _wait_for_entry_lane_idle(db)
