"""Ported from GateoutController::CalculateRate. Where the PHP does something
surprising, the test says so — those cases are pinned deliberately so a future
"cleanup" cannot silently change what customers are charged."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from trafix.rates import (
    TYPE_LOST,
    TYPE_MEMBER_CARD,
    Fee,
    Tariff,
    calculate,
    elapsed,
    format_duration,
    is_active_member,
    within_grace,
)

FLAT = Tariff(
    fee_category="Flat",
    grace_periode=10,
    fee_first_time=60,
    fee_first_price=2000,
    fee_time_1=60,
    fee_price_1=1000,
    fee_price_max=10000,
    ticket_charge=10000,
    stay_charge=10000,
)

PROGRESSIVE = Tariff(
    fee_category="Progresif",
    grace_periode=10,
    fee_first_time=60,
    fee_first_price=3000,
    fee_time_1=60,
    fee_price_1=2000,
    fee_price_max=25000,
    ticket_charge=30000,
    stay_charge=25000,
)

IN = datetime(2026, 7, 24, 3, 30, 0)


def out(**kwargs) -> datetime:
    return IN + timedelta(**kwargs)


def member(*, vehicle_id=1, days_left=365):
    return SimpleNamespace(
        vehicle_id=vehicle_id,
        time_limit=date.today() + timedelta(days=days_left),
        police_number="H4818AI",
        name="Angelo",
    )


# -- grace period -----------------------------------------------------------


@pytest.mark.parametrize("minutes", [0, 1, 9])
def test_inside_grace_is_free(minutes):
    assert calculate(FLAT, IN, out(minutes=minutes)).total == 0


def test_exactly_on_the_grace_boundary_is_free_only_at_zero_seconds():
    """The PHP's odd boundary condition, pinned."""
    assert calculate(FLAT, IN, out(minutes=10)).total == 0
    assert calculate(FLAT, IN, out(minutes=10, seconds=1)).total > 0


def test_just_past_grace_charges():
    assert calculate(FLAT, IN, out(minutes=11)).total == 2000


# -- flat -------------------------------------------------------------------


def test_flat_charges_the_same_regardless_of_hours():
    for hours in (1, 5, 12, 23):
        assert calculate(FLAT, IN, out(hours=hours)).total == 2000


def test_flat_adds_overnight_charge_per_day():
    fee = calculate(FLAT, IN, out(days=1, hours=2))
    assert fee.total == 2000 + 10000


def test_flat_adds_overnight_twice_for_two_days():
    assert calculate(FLAT, IN, out(days=2, hours=1)).total == 2000 + 20000


# -- progressive ------------------------------------------------------------


def test_progressive_first_hour_only():
    assert calculate(PROGRESSIVE, IN, out(minutes=30)).total == 3000
    assert calculate(PROGRESSIVE, IN, out(minutes=59)).total == 3000


def test_progressive_exactly_one_hour_is_first_price():
    assert calculate(PROGRESSIVE, IN, out(hours=1)).total == 3000


def test_progressive_part_of_the_second_hour_adds_one_band():
    assert calculate(PROGRESSIVE, IN, out(hours=1, minutes=1)).total == 5000


def test_progressive_accumulates_per_hour():
    # 3h05m -> first 3000, +2 full extra hours, +1 for the part hour
    assert calculate(PROGRESSIVE, IN, out(hours=3, minutes=5)).total == 3000 + 3 * 2000


def test_progressive_is_capped():
    fee = calculate(PROGRESSIVE, IN, out(hours=23))
    assert fee.total == 25000
    assert "capped" in fee.breakdown


def test_cap_of_zero_means_uncapped():
    uncapped = Tariff(**{**PROGRESSIVE.__dict__, "fee_price_max": 0})
    assert calculate(uncapped, IN, out(hours=23)).total > 25000


# -- the multi-day undercharge (a real defect in the PHP) -------------------


def test_php_behaviour_undercharges_multiday_stays():
    """26 hours is billed as 2, because PHP reads the hour *component*.

    This is what the live system charges today. Pinned so the port stays
    faithful by default.
    """
    fee = calculate(PROGRESSIVE, IN, out(hours=26))
    # 2 h band + one overnight charge, NOT 26 hours of parking
    assert fee.total == 3000 + 2000 + 25000


def test_fix_multiday_charges_the_real_elapsed_hours():
    fee = calculate(PROGRESSIVE, IN, out(hours=26), fix_multiday=True)
    # 26 h would exceed the cap, so it lands on the cap plus the overnight
    assert fee.total == 25000 + 25000


def test_the_two_modes_agree_on_a_same_day_stay():
    a = calculate(PROGRESSIVE, IN, out(hours=5))
    b = calculate(PROGRESSIVE, IN, out(hours=5), fix_multiday=True)
    assert a.total == b.total


# -- members ----------------------------------------------------------------


def test_active_member_parks_free():
    fee = calculate(PROGRESSIVE, IN, out(hours=5), vehicle_id=1, member=member())
    assert fee.total == 0
    assert "member" in fee.breakdown


def test_expired_member_pays():
    expired = member(days_left=-1)
    assert calculate(PROGRESSIVE, IN, out(hours=5), vehicle_id=1, member=expired).total > 0


def test_member_registered_for_a_different_vehicle_class_pays():
    """A motorcycle subscription must not cover a car."""
    bike_member = member(vehicle_id=1)
    fee = calculate(PROGRESSIVE, IN, out(hours=5), vehicle_id=2, member=bike_member)
    assert fee.total > 0


def test_member_card_exit_is_always_free():
    assert calculate(PROGRESSIVE, IN, out(hours=9), type=TYPE_MEMBER_CARD).total == 0


def test_is_active_member_handles_a_missing_member():
    assert is_active_member(None, 1) is False


# -- lost ticket ------------------------------------------------------------


def test_lost_ticket_charges_the_flat_penalty_plus_one_period():
    fee = calculate(PROGRESSIVE, IN, out(minutes=5), type=TYPE_LOST)
    assert fee.total == 30000 + 3000


def test_lost_ticket_for_a_member_is_the_penalty_only():
    fee = calculate(
        PROGRESSIVE, IN, out(minutes=5), type=TYPE_LOST, vehicle_id=1, member=member()
    )
    assert fee.total == 30000


def test_lost_ticket_ignores_duration():
    short = calculate(PROGRESSIVE, IN, out(minutes=1), type=TYPE_LOST)
    long = calculate(PROGRESSIVE, IN, out(days=3), type=TYPE_LOST)
    assert short.total == long.total


# -- duration strings -------------------------------------------------------


def test_duration_matches_the_captured_format():
    """The real API returned '22 m 12 s' for a 22-minute stay."""
    interval = elapsed(IN, IN + timedelta(minutes=22, seconds=12))
    assert format_duration(interval) == "22 m 12 s"


def test_duration_includes_hours_when_present():
    interval = elapsed(IN, IN + timedelta(hours=3, minutes=4, seconds=5))
    assert format_duration(interval) == "3 j 4 m 5 s"


def test_duration_includes_days_when_present():
    interval = elapsed(IN, IN + timedelta(days=2, hours=3, minutes=4, seconds=5))
    assert format_duration(interval) == "2 h 3 j 4 m 5 s"


# -- clock skew -------------------------------------------------------------


def test_a_checkout_before_the_checkin_does_not_go_negative():
    """The entry and exit machines are separate; their clocks drift."""
    fee = calculate(FLAT, IN, IN - timedelta(minutes=30))
    assert fee.total >= 0
    assert elapsed(IN, IN - timedelta(minutes=30)).total_seconds == 1800


# -- unconfigured tariff ----------------------------------------------------


def test_unknown_fee_category_charges_nothing_rather_than_guessing():
    broken = Tariff(**{**FLAT.__dict__, "fee_category": None})
    fee = calculate(broken, IN, out(hours=3))
    assert fee.total == 0
    assert "no fee_category" in fee.breakdown


def test_within_grace_helper():
    assert within_grace(elapsed(IN, out(minutes=5)), 10) is True
    assert within_grace(elapsed(IN, out(hours=1)), 10) is False


def test_fee_formats_as_rupiah():
    assert Fee(9000, "x").formatted() == "Rp9.000"
