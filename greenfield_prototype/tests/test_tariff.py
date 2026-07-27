from datetime import datetime, timedelta

import pytest

from trafix.config import Tariff
from trafix.tariff import calculate, calculate_between, duration_minutes

TARIFF = Tariff(
    currency="IDR",
    grace_minutes=10,
    first_hour=3000,
    next_hour=2000,
    daily_max=25000,
    lost_ticket=50000,
    rounding=500,
)

NO_CAP = Tariff(**{**TARIFF.__dict__, "daily_max": None})
NO_ROUNDING = Tariff(**{**TARIFF.__dict__, "rounding": 0})


@pytest.mark.parametrize("minutes", [0, 1, 9, 10])
def test_within_grace_is_free(minutes):
    assert calculate(TARIFF, minutes).amount == 0


def test_just_past_grace_charges_first_hour():
    assert calculate(TARIFF, 11).amount == 3000


def test_exactly_one_hour_is_first_hour_only():
    assert calculate(TARIFF, 60).amount == 3000


def test_part_of_second_hour_charges_a_full_hour():
    assert calculate(TARIFF, 61).amount == 5000
    assert calculate(TARIFF, 120).amount == 5000
    assert calculate(TARIFF, 121).amount == 7000


def test_daily_cap_applies():
    # 24h uncapped would be 3000 + 23 * 2000 = 49000.
    assert calculate(NO_CAP, 24 * 60).amount == 49000
    assert calculate(TARIFF, 24 * 60).amount == 25000


def test_cap_scales_with_days_started():
    assert calculate(TARIFF, 25 * 60).amount == 50000  # second day started
    assert calculate(TARIFF, 48 * 60).amount == 50000


def test_lost_ticket_is_flat_regardless_of_duration():
    assert calculate(TARIFF, 5, lost_ticket=True).amount == 50000
    assert calculate(TARIFF, 5000, lost_ticket=True).amount == 50000


def test_rounding_up_to_nearest_step():
    odd = Tariff(**{**NO_ROUNDING.__dict__, "first_hour": 2750, "rounding": 500})
    assert calculate(odd, 30).amount == 3000


def test_negative_and_zero_minutes_are_safe():
    assert calculate(TARIFF, -50).amount == 0


def test_duration_clamps_negative_clock_skew():
    entry = datetime(2026, 7, 26, 10, 0)
    assert duration_minutes(entry, entry - timedelta(minutes=5)) == 0


def test_duration_truncates_partial_minutes():
    entry = datetime(2026, 7, 26, 10, 0, 0)
    assert duration_minutes(entry, entry + timedelta(seconds=119)) == 1


def test_calculate_between_matches_calculate():
    entry = datetime(2026, 7, 26, 8, 0)
    exit = entry + timedelta(hours=3, minutes=5)
    assert calculate_between(TARIFF, entry, exit).amount == calculate(TARIFF, 185).amount


def test_fee_carries_duration_and_breakdown():
    fee = calculate(TARIFF, 185)
    assert fee.duration_minutes == 185
    assert fee.currency == "IDR"
    assert "first hour" in fee.breakdown
    assert fee.formatted() == "IDR 9.000"
