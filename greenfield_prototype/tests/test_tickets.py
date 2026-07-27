from datetime import datetime

import pytest

from trafix.tickets import (
    BARCODE_LENGTH,
    barcode_for,
    issue,
    normalize_plate,
    plates_match,
    render_barcode_svg,
    ticket_number,
)

MOMENT = datetime(2026, 7, 26, 14, 30)


def test_ticket_number_format():
    assert ticket_number("in", 123, MOMENT) == "IN-20260726-000123"
    assert ticket_number("out", 7, MOMENT) == "OUT-20260726-000007"


def test_barcode_is_fixed_length_numeric():
    code = barcode_for("in", 123, MOMENT)
    assert code == "260726100123"
    assert len(code) == BARCODE_LENGTH
    assert code.isdigit()


def test_barcode_differs_between_lanes():
    assert barcode_for("in", 1, MOMENT) != barcode_for("out", 1, MOMENT)


def test_barcode_rejects_unknown_lane():
    with pytest.raises(ValueError):
        barcode_for("side", 1, MOMENT)


def test_issue_returns_matching_pair():
    ticket, code = issue("in", 42, MOMENT)
    assert ticket.endswith("000042")
    assert code.endswith("00042")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("b 1234 xyz", "B1234XYZ"),
        ("B-1234-XYZ", "B1234XYZ"),
        ("  b1234xyz  ", "B1234XYZ"),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_normalize_plate(raw, expected):
    assert normalize_plate(raw) == expected


def test_plates_match_ignores_formatting():
    assert plates_match("B 1234 XYZ", "b-1234-xyz") is True


def test_plates_mismatch_detected():
    assert plates_match("B1234XYZ", "D5678ABC") is False


@pytest.mark.parametrize("entry,exit", [(None, "B1"), ("B1", None), ("UNKNOWN", "B1")])
def test_unread_plate_is_not_a_mismatch(entry, exit):
    assert plates_match(entry, exit) is None


def test_barcode_svg_renders():
    svg = render_barcode_svg(barcode_for("in", 1, MOMENT))
    assert svg.lstrip().startswith("<?xml")
    assert "svg" in svg
