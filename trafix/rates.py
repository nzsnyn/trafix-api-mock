"""Parking fee calculation — a port of ``GateoutController::CalculateRate``.

Pure functions: the caller supplies the tariff row, the two timestamps and the
member record. Nothing here touches the database or the clock, which is what
makes the rules arguable with the operations team and testable without a site.

**One deliberate divergence from the PHP.** The original computes the elapsed
time with PHP's ``date_diff`` and then reads ``$diff->format('%h')`` — the
*hours component* of the interval, which resets to 0 every day. A car parked
for 26 hours yields ``%d = 1, %h = 2``, so the progressive band is charged for
2 hours instead of 26, and only the flat ``stay_charge`` covers the rest. That
undercharges every multi-day stay.

``elapsed_hours_php`` reproduces that behaviour and is used by default so this
implementation quotes the same amount the live system does. Passing
``fix_multiday=True`` charges the true elapsed hours instead. Switch it on only
once the operator agrees the prices should change — see :func:`calculate`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

FEE_FLAT = "Flat"
FEE_PROGRESSIVE = "Progresif"

# Calculation modes, mirroring the PHP $type parameter.
TYPE_NORMAL = None
TYPE_LOST = "hilang"  # lost ticket
TYPE_MANUAL = "manual"
TYPE_MEMBER_CARD = "member_card"
TYPE_MEMBER_CARD_FAILED = "member_card_failed"


@dataclass(frozen=True)
class Tariff:
    """The subset of ``parking_fees`` the calculation actually reads."""

    fee_category: str | None
    grace_periode: int
    fee_first_time: float  # minutes; the PHP divides by 60 to get hours
    fee_first_price: float
    fee_time_1: float
    fee_price_1: float
    fee_price_max: float
    ticket_charge: float
    stay_charge: float

    @classmethod
    def from_row(cls, row) -> "Tariff":
        return cls(
            fee_category=row.fee_category,
            grace_periode=int(row.grace_periode or 0),
            fee_first_time=float(row.fee_first_time or 0),
            fee_first_price=float(row.fee_first_price or 0),
            fee_time_1=float(row.fee_time_1 or 0),
            fee_price_1=float(row.fee_price_1 or 0),
            fee_price_max=float(row.fee_price_max or 0),
            ticket_charge=float(row.ticket_charge or 0),
            stay_charge=float(row.stay_charge or 0),
        )


@dataclass(frozen=True)
class Interval:
    """The elapsed time, decomposed the way PHP's DateInterval is."""

    days: int
    hours: int  # 0-23, the component — NOT the total
    minutes: int  # 0-59
    seconds: int  # 0-59
    total_seconds: int

    @property
    def total_hours(self) -> int:
        return self.total_seconds // 3600


@dataclass(frozen=True)
class Fee:
    total: float
    duration: str
    breakdown: str = ""

    def formatted(self, currency: str = "Rp") -> str:
        return f"{currency}{int(self.total):,}".replace(",", ".")


def elapsed(check_in: datetime, check_out: datetime) -> Interval:
    """Decompose the stay.

    ``date_diff`` in PHP is absolute, so a check-out earlier than the check-in
    (clock skew between the entry and exit machines) yields a positive
    interval rather than a negative one. Reproduced here.
    """
    total = int(abs((check_out - check_in).total_seconds()))
    return Interval(
        days=total // 86400,
        hours=(total % 86400) // 3600,
        minutes=(total % 3600) // 60,
        seconds=total % 60,
        total_seconds=total,
    )


def format_duration(interval: Interval) -> str:
    """The Indonesian duration string the cashier screen shows.

    ``h`` = hari (days), ``j`` = jam (hours), ``m`` = menit, ``s`` = detik.
    Branch order is taken from the PHP so the strings match exactly.
    """
    if interval.days > 0:
        return (
            f"{interval.days} h {interval.hours} j "
            f"{interval.minutes} m {interval.seconds} s"
        )
    if interval.hours > 0:
        return f"{interval.hours} j {interval.minutes} m {interval.seconds} s"
    return f"{interval.minutes} m {interval.seconds} s"


def within_grace(interval: Interval, grace_minutes: int) -> bool:
    """Is the stay short enough to be free?

    The PHP condition is oddly written but reduces to: same day, same hour, and
    either under the grace minutes or exactly on it with zero seconds.
    """
    if interval.days or interval.hours:
        return False
    if interval.minutes < grace_minutes:
        return True
    return interval.minutes == grace_minutes and interval.seconds == 0


def is_active_member(
    member, vehicle_id: int | None, today: date | None = None
) -> bool:
    """Members park free while their subscription covers the vehicle class."""
    if member is None:
        return False
    today = today or date.today()
    limit = member.time_limit
    if isinstance(limit, datetime):
        limit = limit.date()
    if limit is None or limit < today:
        return False
    # The PHP compares loosely; vehicle_id may be a string on one side.
    return str(member.vehicle_id) == str(vehicle_id)


def calculate(
    tariff: Tariff,
    check_in: datetime,
    check_out: datetime,
    *,
    vehicle_id: int | None = None,
    member=None,
    type: str | None = TYPE_NORMAL,
    fix_multiday: bool = False,
) -> Fee:
    """Charge for one stay.

    Order of rules, following the PHP:

    1. A lost ticket is a flat charge and no duration is computed.
    2. An active member for this vehicle class pays nothing.
    3. Inside the grace period the stay is free.
    4. Otherwise Flat or Progresif applies, capped at ``fee_price_max``.
    5. ``stay_charge`` is added once per whole day, on top of the cap.

    ``fix_multiday`` corrects the PHP's use of the hour *component* rather than
    total hours. See the module docstring before turning it on.
    """
    interval = elapsed(check_in, check_out)
    duration = format_duration(interval)

    # 1. Lost ticket: a member pays only the ticket charge, others also pay
    #    one parking period on top.
    if type == TYPE_LOST:
        if member is not None and _member_in_date(member):
            return Fee(tariff.ticket_charge, "0", "lost ticket, member")
        return Fee(
            tariff.ticket_charge + tariff.fee_first_price,
            "0",
            "lost ticket + one parking period",
        )

    # 2. A card-based member exit is always free.
    if type == TYPE_MEMBER_CARD:
        return Fee(0, duration, "member card")

    # 3. Active member for this vehicle class.
    if is_active_member(member, vehicle_id):
        return Fee(0, duration, "active member")

    # 4. Grace period.
    if within_grace(interval, tariff.grace_periode):
        return Fee(0, duration, f"within {tariff.grace_periode} min grace")

    hours = interval.total_hours if fix_multiday else interval.hours
    total, breakdown = _charge(tariff, hours, interval.minutes)

    # 5. Overnight penalty, added after the cap.
    if interval.days > 0 and tariff.stay_charge > 0:
        total += tariff.stay_charge * interval.days
        breakdown += f" + {interval.days} x overnight {int(tariff.stay_charge)}"

    return Fee(total, duration, breakdown)


def _charge(tariff: Tariff, hours: int, minutes: int) -> tuple[float, str]:
    if tariff.fee_category == FEE_FLAT:
        return tariff.fee_first_price, f"flat {int(tariff.fee_first_price)}"

    if tariff.fee_category != FEE_PROGRESSIVE:
        # Unconfigured tariff: charge nothing rather than guess.
        return 0.0, "no fee_category configured"

    first_hours = tariff.fee_first_time / 60
    total = tariff.fee_first_price
    breakdown = f"first {int(tariff.fee_first_price)}"

    if hours == first_hours:
        if minutes > 0:
            total += tariff.fee_price_1
            breakdown += f" + 1 x {int(tariff.fee_price_1)}"
    elif hours > first_hours:
        extra = int(hours - first_hours)
        total += extra * tariff.fee_price_1
        breakdown += f" + {extra} x {int(tariff.fee_price_1)}"
        if minutes > 0:
            total += tariff.fee_price_1
            breakdown += f" + 1 x {int(tariff.fee_price_1)} (part hour)"

    if tariff.fee_price_max > 0 and total > tariff.fee_price_max:
        return tariff.fee_price_max, f"capped at {int(tariff.fee_price_max)}"

    return total, breakdown


def _member_in_date(member, today: date | None = None) -> bool:
    """Membership valid today, regardless of vehicle class.

    The lost-ticket branch in the PHP checks only ``time_limit``, unlike the
    normal path which also matches the vehicle. Reproduced.
    """
    today = today or date.today()
    limit = member.time_limit
    if isinstance(limit, datetime):
        limit = limit.date()
    return limit is not None and limit >= today
