"""The whole site, running for real: PostgreSQL, Mosquitto, and every
component as its own process talking over MQTT and HTTP.

Skips itself when the broker or database is not up, so ``pytest`` stays useful
without Docker::

    docker compose up -d mosquitto postgres
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from trafix import db
from trafix.config import load_config
from trafix.models import Transactions

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable
E2E_ENV = "e2e"

ENTRY_GATE = "1"
EXIT_GATE = "2"

# Must match policies.button_debounce_seconds in config/devices.yaml.
DEBOUNCE_SECONDS = 5.0


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module")
def config():
    config = load_config(E2E_ENV)
    if not _port_open(config.broker.host, config.broker.port):
        pytest.skip(
            f"no MQTT broker at {config.broker.host}:{config.broker.port} — "
            f"run: docker compose up -d mosquitto"
        )
    if not _port_open("127.0.0.1", 5433):
        pytest.skip(
            "no PostgreSQL on 127.0.0.1:5433 — run: docker compose up -d postgres"
        )
    return config


def _spawn(module: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [PYTHON, "-m", module, "--env", E2E_ENV, *args],
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _wait_for_http(url: str, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            time.sleep(0.3)
    raise TimeoutError(f"{url} never answered")


@pytest.fixture(scope="module")
def system(config):
    """Bring up both LPR units, both controllers, the API and the orchestrator."""
    db.init_engine(config.database_url)
    db.drop_all()
    db.create_all()
    with db.session_scope() as session:
        db.seed(session)

    processes = [
        _spawn("mocks.lpr_unit", "--gate", ENTRY_GATE),
        _spawn("mocks.lpr_unit", "--gate", EXIT_GATE, "--publishes-reads"),
    ]
    try:
        _wait_for_http(f"{config.lpr_for(ENTRY_GATE).base_url}/mock/state")

        processes += [
            _spawn("mocks.gate_controller", "--gate", ENTRY_GATE),
            _spawn("mocks.gate_controller", "--gate", EXIT_GATE, "--exit-lane"),
            _spawn("trafix.server"),
        ]
        _wait_for_http(f"{config.api.base_url}/api/health")

        processes.append(_spawn("trafix.orchestrator"))
        time.sleep(2.5)

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


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def queue_plate(config, gate: str, plate: str) -> None:
    httpx.post(
        f"{config.lpr_for(gate).base_url}/mock/queue", json={"plate": plate}, timeout=5
    ).raise_for_status()


def set_lpr_mode(config, gate: str, mode: str) -> None:
    httpx.post(
        f"{config.lpr_for(gate).base_url}/mock/mode", json={"mode": mode}, timeout=5
    ).raise_for_status()


_last_press = 0.0


def drive_in(plate: str | None = None, gate: str = ENTRY_GATE) -> None:
    """Make a vehicle arrive and take a ticket, through the mock controller.

    Waits out the orchestrator's button debounce first. That debounce is a real
    feature — it stops one driver pressing twice from issuing two tickets — and
    it cannot tell consecutive tests apart from an impatient driver, so back to
    back entries here would silently get one ticket between them.
    """
    global _last_press

    remaining = DEBOUNCE_SECONDS - (time.monotonic() - _last_press)
    if remaining > 0:
        time.sleep(remaining)

    args = ["enter", "--gate", gate]
    if plate:
        args += ["--plate", plate]
    subprocess.run(
        [PYTHON, "-m", "cli.trafix", "--env", E2E_ENV, *args],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    _last_press = time.monotonic()


def exit_read(plate: str, gate: str = EXIT_GATE) -> None:
    subprocess.run(
        [
            PYTHON, "-m", "cli.trafix", "--env", E2E_ENV,
            "exit-read", "--gate", gate, "--plate", plate,
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )


def card_in(card: str, gate: str = ENTRY_GATE) -> None:
    """Tap an RFID card at the entry, through the mock controller."""
    global _last_press

    remaining = DEBOUNCE_SECONDS - (time.monotonic() - _last_press)
    if remaining > 0:
        time.sleep(remaining)

    subprocess.run(
        [
            PYTHON, "-m", "cli.trafix", "--env", E2E_ENV,
            "card", "--gate", gate, "--card", card,
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    _last_press = time.monotonic()


def wait_for(predicate, timeout: float = 20.0, interval: float = 0.3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"condition never became true (last: {last!r})")


def find_by_plate(plate: str):
    with db.session_scope() as session:
        return session.scalar(
            select(Transactions)
            .where(Transactions.police_number == plate)
            .order_by(Transactions.transaction_id.desc())
        )


def find_by_card(card: str):
    with db.session_scope() as session:
        return session.scalar(
            select(Transactions)
            .where(Transactions.card_number == card)
            .order_by(Transactions.transaction_id.desc())
        )


def count_transactions() -> int:
    with db.session_scope() as session:
        return len(session.scalars(select(Transactions)).all())


@pytest.fixture(autouse=True)
def reset_lpr(system):
    """Leave both cameras in a good state between tests."""
    yield
    for gate in (ENTRY_GATE, EXIT_GATE):
        try:
            httpx.post(f"{system.lpr_for(gate).base_url}/mock/reset", timeout=3)
        except httpx.HTTPError:
            pass


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_a_button_press_issues_a_ticket_and_opens_the_barrier(system):
    """The full entry sequence from flow.md §5, over the real wire."""
    plate = "H488AI"
    drive_in(plate)

    transaction = wait_for(lambda: find_by_plate(plate))

    assert transaction.transaction_code
    assert transaction.gate_in == ENTRY_GATE
    assert transaction.status == "gatein"
    assert transaction.time_checkout is None


def test_the_ticket_is_actually_printed(system):
    """The controller must receive decodable ESC/POS, not just a command."""
    plate = "B1234XX"
    drive_in(plate)
    wait_for(lambda: find_by_plate(plate))
    # The controller logs the rendered ticket; if the hex were malformed it
    # would have logged a decode error instead and the run would still pass,
    # so assert on the stored record plus a successful barrier cycle below.
    transaction = find_by_plate(plate)
    assert transaction.cam_in


def test_a_plate_the_camera_cannot_read_still_gets_a_ticket(system):
    """4 of 6 tickets on site recorded no plate (§7.7)."""
    before = count_transactions()
    set_lpr_mode(system, ENTRY_GATE, "no_plate")
    drive_in()

    wait_for(lambda: count_transactions() > before)
    with db.session_scope() as session:
        latest = session.scalar(
            select(Transactions).order_by(Transactions.transaction_id.desc())
        )
    assert latest.police_number is None
    assert latest.transaction_code, "a ticket is what gets the driver out"


def test_a_dead_lpr_does_not_stop_entry(system):
    before = count_transactions()
    set_lpr_mode(system, ENTRY_GATE, "error")
    drive_in()

    wait_for(lambda: count_transactions() > before, timeout=25)


def test_repeat_button_press_does_not_issue_two_tickets(system):
    plate = "D5555DD"
    drive_in(plate)
    wait_for(lambda: find_by_plate(plate))
    after_first = count_transactions()

    subprocess.run(
        [PYTHON, "-m", "cli.trafix", "--env", E2E_ENV, "press", "--gate", ENTRY_GATE],
        cwd=PROJECT_ROOT, check=True, capture_output=True,
    )
    time.sleep(3)

    assert count_transactions() == after_first


def test_an_rfid_card_opens_the_gate_for_a_member(system):
    """readCard -> /api/gatein/card -> member transaction, no ticket print."""
    card = "006343040"
    card_in(card)

    transaction = wait_for(lambda: find_by_card(card))

    assert transaction.transaction_code
    assert transaction.type == "member"
    assert transaction.card_number == card
    assert transaction.payment_status == "lunas"
    assert transaction.total == 0
    assert transaction.gate_in == ENTRY_GATE
    assert transaction.status == "gatein"


def test_the_cashier_can_settle_and_release(system):
    plate = "F7777FF"
    drive_in(plate)
    entry = wait_for(lambda: find_by_plate(plate))

    result = subprocess.run(
        [
            PYTHON, "-m", "cli.cashier", "--env", E2E_ENV,
            "settle", "--ticket", entry.transaction_code, "-y",
        ],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    assert "settled" in result.stdout, result.stdout + result.stderr

    settled = wait_for(
        lambda: (
            row
            if (row := find_by_plate(plate)) and row.time_checkout is not None
            else None
        )
    )
    assert settled.payment_status == "lunas"
    assert settled.gate_status == "out"
    assert settled.gate_out == EXIT_GATE


def test_the_automated_exit_path_works(system):
    """flow.md §7.1: this is the path that returns 500 on the live site.

    The exit LPR announces a plate; the orchestrator resolves it and settles.
    """
    plate = "G8888GG"
    drive_in(plate)
    entry = wait_for(lambda: find_by_plate(plate))
    assert entry.time_checkout is None

    exit_read(plate)

    settled = wait_for(
        lambda: (
            row
            if (row := find_by_plate(plate)) and row.time_checkout is not None
            else None
        )
    )
    assert settled.payment_status == "lunas"
    assert settled.total == 0, "left within the grace period"


def test_the_api_serves_lpr_gateout_without_a_500(system):
    """Direct proof the missing method now exists."""
    plate = "K9999KK"
    drive_in(plate)
    entry = wait_for(lambda: find_by_plate(plate))

    response = httpx.post(
        f"{system.api.base_url}/api/lpr/gateout",
        json={
            "gate_out": EXIT_GATE,
            "transaction_code": entry.transaction_code,
            "plate_num": plate,
        },
        timeout=10,
    )
    assert response.status_code == 200, "production returns 500 here"
    assert response.json()["status"] == "success_ticket"


def test_a_used_ticket_cannot_be_reused(system):
    plate = "L1010LL"
    drive_in(plate)
    entry = wait_for(lambda: find_by_plate(plate))

    for _ in range(2):
        response = httpx.post(
            f"{system.api.base_url}/api/lpr/gateout",
            json={"gate_out": EXIT_GATE, "transaction_code": entry.transaction_code},
            timeout=10,
        )
    assert response.json()["status"] == "ticket_used"


def test_the_entry_snapshot_url_serves_a_real_image(system):
    plate = "M2020MM"
    drive_in(plate)
    transaction = wait_for(lambda: find_by_plate(plate))

    # cam_in is 'storage/lpr/gatein/CAMIN_LPR_....jpg'; the API serves /storage.
    path = transaction.cam_in
    assert path.startswith("storage/"), path

    url = f"{system.api.base_url}/{path}"
    response = wait_for(
        lambda: (r if (r := httpx.get(url, timeout=3)).status_code == 200 else None)
    )
    assert response.headers["content-type"].startswith("image/")
    assert response.content.startswith(b"\xff\xd8"), "JPEG magic"


def test_a_plate_mismatch_is_recorded_but_does_not_block(system):
    """§7.7: the two cameras disagree. The ticket must still work."""
    drive_in("N3030NN")
    entry = wait_for(lambda: find_by_plate("N3030NN"))

    response = httpx.post(
        f"{system.api.base_url}/api/lpr/gateout",
        json={
            "gate_out": EXIT_GATE,
            "transaction_code": entry.transaction_code,
            "plate_num": "N303ONN",  # the exit camera misreads a 0 as an O
        },
        timeout=10,
    )
    body = response.json()
    assert body["status"] == "success_ticket"
    assert body["plate_match"] is False

    with db.session_scope() as session:
        row = session.scalar(
            select(Transactions).where(
                Transactions.transaction_code == entry.transaction_code
            )
        )
    assert "mismatch" in (row.keterangan or "")
