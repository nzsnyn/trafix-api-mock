"""Parking fee calculation.

Pure functions: no database, no network, no clock of its own. Everything the
calculation needs is passed in, which is what makes the rules easy to test and
easy to argue about with the operations team.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from trafix.config import Tariff

MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class Fee:
    amount: int
    currency: str
    duration_minutes: int
    breakdown: str

    def formatted(self) -> str:
        return f"{self.currency} {self.amount:,}".replace(",", ".")


def duration_minutes(entry: datetime, exit: datetime) -> int:
    """Whole minutes parked, never negative.

    A clock skew between the entry and exit terminals must not produce a
    negative stay, so the result is clamped at zero.
    """
    delta = (exit - entry).total_seconds()
    return max(0, int(delta // 60))


def calculate(tariff: Tariff, minutes: int, *, lost_ticket: bool = False) -> Fee:
    """Charge for a stay of ``minutes``.

    Rules, in order:
      1. A lost ticket is a flat charge, regardless of duration.
      2. Inside the grace period the stay is free.
      3. Otherwise: the first hour, plus every started hour after it.
      4. The total is capped per 24 hours started, then rounded up.
    """
    minutes = max(0, int(minutes))

    if lost_ticket:
        amount = _round_up(tariff.lost_ticket, tariff.rounding)
        return Fee(amount, tariff.currency, minutes, "lost ticket flat rate")

    if minutes <= tariff.grace_minutes:
        return Fee(0, tariff.currency, minutes, f"within {tariff.grace_minutes} min grace")

    hours_started = max(1, math.ceil(minutes / 60))
    extra_hours = hours_started - 1
    amount = tariff.first_hour + extra_hours * tariff.next_hour
    breakdown = f"first hour {tariff.first_hour}"
    if extra_hours:
        breakdown += f" + {extra_hours} x {tariff.next_hour}"

    if tariff.daily_max is not None:
        days_started = max(1, math.ceil(minutes / MINUTES_PER_DAY))
        cap = tariff.daily_max * days_started
        if amount > cap:
            amount = cap
            breakdown = f"daily cap {tariff.daily_max} x {days_started} day(s)"

    rounded = _round_up(amount, tariff.rounding)
    if rounded != amount:
        breakdown += f", rounded up to {tariff.rounding}"

    return Fee(rounded, tariff.currency, minutes, breakdown)


def calculate_between(
    tariff: Tariff, entry: datetime, exit: datetime, *, lost_ticket: bool = False
) -> Fee:
    """Convenience wrapper over :func:`calculate` for two timestamps."""
    return calculate(tariff, duration_minutes(entry, exit), lost_ticket=lost_ticket)


def _round_up(amount: int, step: int) -> int:
    if step <= 0:
        return amount
    return int(math.ceil(amount / step) * step)
