"""Shared fixtures.

Service-level tests run against SQLite so they need no Docker. The schema is
identical; only the engine differs. The end-to-end tests use the real
PostgreSQL and skip themselves when it is not running.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from trafix.models import Base, Locations, ParkingFees
from trafix.service import NullPublisher, ParkingService


class FakeClock:
    """A clock the test advances by hand."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 7, 24, 3, 30, 0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> datetime:
        self.now += timedelta(**kwargs)
        return self.now


@pytest.fixture
def session_factory():
    """An isolated in-memory database with the site's reference data."""
    from trafix.db import seed

    # StaticPool + check_same_thread=False: an in-memory SQLite database lives
    # in its connection, and the API tests run the app on another thread.
    # PostgreSQL has neither restriction, so this is test scaffolding only.
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    with factory() as session:
        seed(session)

    yield factory
    engine.dispose()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def publisher() -> NullPublisher:
    return NullPublisher()


@pytest.fixture
def service(session_factory, publisher, clock) -> ParkingService:
    # print_gap_seconds=0: the real 200 ms pause between ticket halves matters
    # to the printer, not to the logic, and it makes the suite 40x slower.
    return ParkingService(
        session_factory, publisher=publisher, clock=clock, print_gap_seconds=0
    )


@pytest.fixture
def flat_tariff(session_factory):
    """Make both vehicle classes flat-rate, for predictable amounts."""
    with session_factory() as session:
        for row in session.scalars(select(ParkingFees)).all():
            row.fee_category = "Flat"
            row.fee_first_price = 2000
        session.commit()


@pytest.fixture
def qris_site(session_factory):
    """Activate the location so the Xendit QRIS branch is taken."""
    with session_factory() as session:
        location = session.scalar(select(Locations))
        location.status = "active"
        location.id_userlocations = "loc-1"
        session.commit()
