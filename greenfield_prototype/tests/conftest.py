"""Shared fakes. These let the state machines be tested with no broker,
no camera and no clock of their own."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trafix.config import Config, BrokerConfig, LprConfig, ManlessConfig, Policies, Tariff
from trafix.db import Database
from trafix.envelope import Envelope
from trafix.lpr_client import PlateRead

START = datetime(2026, 7, 26, 8, 0, tzinfo=timezone.utc)


class FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, start: datetime = START) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


class FakeCamera:
    """Stands in for :class:`trafix.lpr_client.LprClient`.

    ``next_reads`` is consumed in order; once it is empty ``default`` is
    returned for every further capture.
    """

    def __init__(self, default: PlateRead | None = None) -> None:
        self.default = default or PlateRead(
            ok=True, plate="B1234XYZ", confidence=0.93,
            image_url="http://cam/images/x.jpg",
        )
        self.next_reads: list[PlateRead] = []
        self.calls: list[dict] = []

    def queue(self, read: PlateRead) -> None:
        self.next_reads.append(read)

    def capture(self, *, trigger: str = "gate", lane: str | None = None) -> PlateRead:
        self.calls.append({"trigger": trigger, "lane": lane})
        if self.next_reads:
            return self.next_reads.pop(0)
        return self.default


class FakePublisher:
    """Captures the commands the server would have sent to a terminal."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Envelope]] = []

    def __call__(self, lane: str, message: Envelope) -> None:
        self.sent.append((lane, message))

    @property
    def types(self) -> list[str]:
        return [message.type for _lane, message in self.sent]

    def last(self, type: str | None = None) -> Envelope:
        for _lane, message in reversed(self.sent):
            if type is None or message.type == type:
                return message
        raise AssertionError(f"no {type or 'command'} was sent; got {self.types}")

    def has(self, type: str) -> bool:
        return type in self.types

    def clear(self) -> None:
        self.sent.clear()


def make_config(**policy_overrides) -> Config:
    policies = Policies(
        lpr_failure="allow",
        plate_mismatch="flag",
        button_debounce_seconds=5,
        lpr_timeout_seconds=1.0,
        lpr_retries=0,
        lpr_min_confidence=0.55,
    )
    if policy_overrides:
        policies = Policies(**{**policies.__dict__, **policy_overrides})

    def lpr(lane: str, port: int) -> LprConfig:
        return LprConfig(
            name=f"lpr_{lane}", lane=lane, host="127.0.0.1", port=port,
            base_url=f"http://127.0.0.1:{port}", public_url=f"http://127.0.0.1:{port}",
        )

    return Config(
        env="test",
        broker=BrokerConfig(host="127.0.0.1", port=1883, keepalive=30,
                            client_id_prefix="test", topic_root="trafix-test"),
        manless={
            "in": ManlessConfig(name="manless_in", lane="in", host="127.0.0.1"),
            "out": ManlessConfig(name="manless_out", lane="out", host="127.0.0.1"),
        },
        lpr={"in": lpr("in", 8130), "out": lpr("out", 8149)},
        policies=policies,
        tariff=Tariff(
            currency="IDR", grace_minutes=10, first_hour=3000, next_hour=2000,
            daily_max=25000, lost_ticket=50000, rounding=500,
        ),
        database=":memory:",
    )


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def camera() -> FakeCamera:
    return FakeCamera()


@pytest.fixture
def publisher() -> FakePublisher:
    return FakePublisher()


def evt(type: str, device_id: str = "manless_in", **payload) -> Envelope:
    return Envelope(type=type, device_id=device_id, payload=payload)
