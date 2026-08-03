"""Database engine, sessions, and the seed data for the Salatiga site."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date, timedelta
from typing import Iterator

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from trafix.models import (
    Admins,
    Base,
    Locations,
    Members,
    ParkingFees,
    Vehicles,
)

log = logging.getLogger(__name__)

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def init_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create the engine and session factory. Call once per process."""
    global _engine, _Session
    _engine = create_engine(database_url, echo=echo, pool_pre_ping=True, future=True)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("init_engine() has not been called")
    return _engine


def create_all() -> None:
    Base.metadata.create_all(get_engine())


def drop_all() -> None:
    Base.metadata.drop_all(get_engine())


def new_session() -> Session:
    if _Session is None:
        raise RuntimeError("init_engine() has not been called")
    return _Session()


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transactional scope: commits on success, rolls back on error."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

SITE_NAME = "Sinode GKJ"
SITE_ADDRESS = "Jl. Dr. Sumardi No. 8, Kec. Sidorejo, Kota Salatiga, Jawa Tengah"

VEHICLE_MOTOR = 1
VEHICLE_MOBIL = 2


def seed(session: Session, *, include_demo_member: bool = True) -> None:
    """Populate reference tables with the real site's configuration.

    Site name, address and the ticket footer amounts are the ones decoded from
    the printed ticket (flow.md §5). The tariff *structure* is real; the
    progressive band prices are ❓ not visible in the capture, so plausible
    values are used — replace them from the live ``parking_fees`` table before
    trusting any fee this system quotes.
    """
    if session.scalar(select(Locations).limit(1)) is None:
        session.add(
            Locations(
                name=SITE_NAME,
                address=SITE_ADDRESS,
                phone="",
                # 'active' would enable the Xendit QRIS path. The captured
                # tickets say "Simpan QR untuk Keluar Parkir", which is the
                # cash branch, so the live location is not active.
                status="inactive",
                id_userlocations=None,
            )
        )

    if session.scalar(select(Vehicles).limit(1)) is None:
        session.add_all(
            [
                Vehicles(vehicle_id=VEHICLE_MOTOR, name="Motor", actived="1"),
                Vehicles(vehicle_id=VEHICLE_MOBIL, name="Mobil", actived="1"),
            ]
        )

    if session.scalar(select(ParkingFees).limit(1)) is None:
        session.add_all(
            [
                ParkingFees(
                    vehicle_id=VEHICLE_MOTOR,
                    fee_category="Flat",
                    grace_periode=10,
                    fee_first_time="60",
                    fee_first_price=2000,
                    fee_time_1="60",
                    fee_price_1=1000,
                    fee_price_max=10000,
                    # These two are printed on the real ticket.
                    ticket_charge=10000,
                    stay_charge=10000,
                ),
                ParkingFees(
                    vehicle_id=VEHICLE_MOBIL,
                    fee_category="Progresif",
                    grace_periode=10,
                    fee_first_time="60",
                    fee_first_price=3000,
                    fee_time_1="60",
                    fee_price_1=2000,
                    fee_time_2="60",
                    fee_price_2=2000,
                    fee_time_3="60",
                    fee_price_3=2000,
                    fee_price_max=25000,
                    ticket_charge=30000,
                    stay_charge=25000,
                ),
            ]
        )

    if session.scalar(select(Admins).limit(1)) is None:
        session.add(Admins(admin_id=1, name="Kasir 1", username="kasir1", level="cashier"))

    if include_demo_member and session.scalar(select(Members).limit(1)) is None:
        # 'Angelo' / H4818AI is real data from the captured API response.
        # card_number 006343040 is the RFID tag observed on the wire
        # (flow.md §5, readCard) — the demo member owns it so member
        # auto-entry can be exercised end to end.
        session.add(
            Members(
                name="Angelo",
                phone="",
                email="",
                police_number="H4818AI",
                member_code="H4818AI",
                card_number="006343040",
                subscription_period="monthly",
                time_limit=date.today() + timedelta(days=365),
                vehicle_id=VEHICLE_MOTOR,
            )
        )

    session.commit()
    log.info("reference data seeded")
