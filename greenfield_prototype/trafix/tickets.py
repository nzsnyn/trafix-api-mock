"""Ticket numbers, barcodes, and plate normalisation.

The barcode is what the driver physically carries back to the exit, so it is
kept all-numeric and fixed-length: Code128 handles it, and so does every cheap
handheld scanner.
"""

from __future__ import annotations

import re
from datetime import datetime

BARCODE_LENGTH = 12

# Lane codes embedded in the ticket number, so a ticket is traceable to a gate.
_LANE_CODE = {"in": "1", "out": "2"}

_PLATE_CLEAN = re.compile(r"[^A-Z0-9]")

PLATE_UNKNOWN = "UNKNOWN"


def ticket_number(lane: str, sequence: int, moment: datetime) -> str:
    """Human-readable ticket identifier, e.g. ``IN-20260726-000123``."""
    return f"{lane.upper()}-{moment:%Y%m%d}-{sequence:06d}"


def barcode_for(lane: str, sequence: int, moment: datetime) -> str:
    """All-numeric barcode: ``YYMMDD`` + lane digit + 5-digit sequence.

    Twelve digits total, unique for up to 99,999 tickets per lane per day.
    """
    lane_code = _LANE_CODE.get(lane)
    if lane_code is None:
        raise ValueError(f"unknown lane {lane!r}")
    body = f"{moment:%y%m%d}{lane_code}{sequence % 100000:05d}"
    return body.ljust(BARCODE_LENGTH, "0")[:BARCODE_LENGTH]


def issue(lane: str, sequence: int, moment: datetime) -> tuple[str, str]:
    """Return ``(ticket_no, barcode)`` for a new entry."""
    return ticket_number(lane, sequence, moment), barcode_for(lane, sequence, moment)


def normalize_plate(plate: str | None) -> str | None:
    """Strip spaces, dashes and case so two reads can be compared.

    ``"b 1234 xyz"`` and ``"B-1234-XYZ"`` are the same vehicle.
    """
    if plate is None:
        return None
    cleaned = _PLATE_CLEAN.sub("", plate.upper())
    return cleaned or None


def plates_match(entry_plate: str | None, exit_plate: str | None) -> bool | None:
    """Compare two reads.

    Returns ``None`` when the comparison is meaningless because at least one
    side was never read — that is not a mismatch, and must not be treated as
    one.
    """
    left = normalize_plate(entry_plate)
    right = normalize_plate(exit_plate)
    if left in (None, PLATE_UNKNOWN) or right in (None, PLATE_UNKNOWN):
        return None
    return left == right


def render_barcode_svg(barcode: str) -> str:
    """Render the barcode as Code128 SVG, for a printer that accepts vectors.

    The mock terminal only logs it, but the real printer driver will want it,
    and generating it here keeps that knowledge in one place.
    """
    import io

    import barcode as barcode_lib
    from barcode.writer import SVGWriter

    buffer = io.BytesIO()
    code = barcode_lib.get("code128", barcode, writer=SVGWriter())
    code.write(buffer, options={"module_height": 12.0, "font_size": 8, "quiet_zone": 2.0})
    return buffer.getvalue().decode("utf-8")
