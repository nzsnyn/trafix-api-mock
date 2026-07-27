"""The Parkways database, mirrored from the Laravel migrations.

Column names, nullability and types follow
``Trafix/mqtt_app_lpr/database/migrations`` so this schema can sit alongside —
or eventually replace — the PHP application without a data migration.

Two Laravel conventions are preserved deliberately, because production data
already looks like this:

* ``time_checkin`` / ``time_checkout`` are **strings**, not timestamps. The
  original migration declares ``$table->string(...)`` and the app writes
  ``Y-m-d H:i:s``. Changing the type here would break reads of existing rows.
* ``transaction_id`` is the primary key, not ``id``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Values the application writes into transactions.status / gate_status /
# payment_status. Collected from GateController and GateoutController.
STATUS_GATEIN = "gatein"
GATE_STATUS_IN = "in"
GATE_STATUS_OUT = "out"
PAYMENT_PAID = "lunas"  # "paid"
PAYMENT_UNPAID = "belum_lunas"

TYPE_QR_PAYMENT = "payment"
TYPE_QR_CASH = "cash"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Transactions(Base):
    """One parking session. The centre of the whole system."""

    __tablename__ = "transactions"

    transaction_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_code: Mapped[str] = mapped_column(String(255), index=True)

    # Strings, matching the Laravel migration. Format: 'Y-m-d H:i:s'.
    time_checkin: Mapped[str] = mapped_column(String(255))
    time_checkout: Mapped[str | None] = mapped_column(String(255), nullable=True)

    vehicle_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    persons: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total: Mapped[float | None] = mapped_column(Double, nullable=True)
    police_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    admin_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shift_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Image paths. cam_* is the CCTV capture, camin_lpr/camout_lpr the LPR one.
    cam_in: Mapped[str] = mapped_column(String(255), default="-")
    camin_lpr: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cam_out: Mapped[str | None] = mapped_column(String(255), nullable=True)
    camout_lpr: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cam_payment: Mapped[str | None] = mapped_column(String(255), nullable=True)

    type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payment_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    payment_type: Mapped[str | None] = mapped_column(String(10), default="cash")

    gate_in: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gate_out: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gate_status: Mapped[str | None] = mapped_column(String(255), nullable=True)

    card_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    qrcode: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pr_id_xendit: Mapped[str | None] = mapped_column(String(255), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    keterangan: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded: Mapped[str | None] = mapped_column(String(255), default="0")
    id_userlocations: Mapped[str | None] = mapped_column(String(255), nullable=True)
    id_transactionlocations: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_now, onupdate=_now
    )

    __table_args__ = (
        Index("idx_tx_code", "transaction_code"),
        Index("idx_tx_qrcode", "qrcode"),
        Index("idx_tx_plate", "police_number"),
        Index("idx_tx_gate_status", "gate_status"),
        Index("idx_tx_card", "card_number"),
    )

    def is_inside(self) -> bool:
        """Is the vehicle still in the car park?

        flow.md §7.7 flags that the real code is inconsistent here:
        ``GateOutRfidLpr`` filters on ``gate_status='in'`` while
        ``checkLprImageGateOut`` filters on ``time_checkout IS NULL``. We treat
        **``time_checkout IS NULL`` as authoritative** — it is set exactly once,
        at the moment of exit, whereas ``gate_status`` is nullable and was only
        added in 2024, so older rows have it empty.
        """
        return self.time_checkout is None

    def is_paid(self) -> bool:
        return self.payment_status == PAYMENT_PAID


class ParkingFees(Base):
    """Tariff per vehicle type. Drives CalculateRate."""

    __tablename__ = "parking_fees"

    parking_fee_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(Integer, index=True)

    # 'Flat' or 'Progresif'. Compared case-sensitively by the PHP.
    fee_category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    grace_periode: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # fee_*_time columns are strings of minutes; the PHP divides them by 60 to
    # get hours. Kept as strings to match the migration.
    fee_first_time: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fee_first_price: Mapped[float | None] = mapped_column(Double, nullable=True)
    fee_time_1: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fee_price_1: Mapped[float | None] = mapped_column(Double, nullable=True)
    fee_time_2: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fee_price_2: Mapped[float | None] = mapped_column(Double, nullable=True)
    fee_time_3: Mapped[str | None] = mapped_column(String(255), nullable=True)
    fee_price_3: Mapped[float | None] = mapped_column(Double, nullable=True)
    fee_price_max: Mapped[float | None] = mapped_column(Double, nullable=True)

    ticket_charge: Mapped[float | None] = mapped_column(Double, nullable=True)
    stay_charge: Mapped[float | None] = mapped_column(Double, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Members(Base):
    """Subscribers who park free until ``time_limit``."""

    __tablename__ = "members"

    member_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(255), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    police_number: Mapped[str] = mapped_column(String(255), index=True)
    member_code: Mapped[str] = mapped_column(String(255), index=True)
    subscription_period: Mapped[str] = mapped_column(String(255), default="")
    time_limit: Mapped[Date] = mapped_column(Date)
    vehicle_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    id_userlocations: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Locations(Base):
    """The site. Its name and address are printed on every ticket."""

    __tablename__ = "locations"

    location_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    address: Mapped[str] = mapped_column(String(255))
    phone: Mapped[str] = mapped_column(String(255), default="")
    image: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 'active' enables the Xendit QRIS path in gatein().
    status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    id_userlocations: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Vehicles(Base):
    """Vehicle classes. On this site: 1 = Motor, 2 = Mobil."""

    __tablename__ = "vehicles"

    vehicle_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    actived: Mapped[str] = mapped_column(String(255), default="1")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Admins(Base):
    """Cashier accounts."""

    __tablename__ = "admins"

    admin_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(255), index=True)
    password: Mapped[str] = mapped_column(String(255), default="")
    level: Mapped[str | None] = mapped_column(String(255), nullable=True)
    id_userlocations: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class XenditQrPool(Base):
    """Pre-generated QRIS codes, drawn from at check-in.

    The pool is refilled asynchronously; flow.md §2 warns that without a queue
    worker it silently runs dry and every ticket falls back to cash.
    """

    __tablename__ = "xendit_qr_pool"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(Integer, index=True)
    qr_string: Mapped[str] = mapped_column(Text)
    pr_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="available", index=True)
    expired_at: Mapped[datetime] = mapped_column(DateTime)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class GateEvents(Base):
    """Audit log. Not in the Laravel schema — added here.

    The production system keeps no record of what the gate hardware did, which
    is why flow.md had to be reconstructed from a packet capture. Every MQTT
    message and gate decision is recorded here so the next person does not need
    Wireshark.
    """

    __tablename__ = "gate_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    source: Mapped[str] = mapped_column(String(64))
    gate: Mapped[str | None] = mapped_column(String(16), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)
    method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transaction_code: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
