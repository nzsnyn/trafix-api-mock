"""Ticket construction — a port of ``TicketPrinterService.php``.

The gate controller does not print text. It prints raw ESC/POS bytes handed to
it as a **hex string** inside a ``txUartData`` message, so the payload is
doubly encoded: JSON → ``data[].uartData`` is hex → the hex decodes to printer
bytes (flow.md §8).

Byte-for-byte compatibility with the PHP original matters: this output goes to
a real thermal printer, and the decoded ticket in flow.md §5 is the reference.
Where the PHP does something surprising it is reproduced, with a note.
"""

from __future__ import annotations

import textwrap
from dataclasses import dataclass

# ESC/POS control sequences, as hex, exactly as the PHP emits them.
ESC_ALIGN_CENTER = "1B6101"  # ESC a 1  — centre the following line
CRLF = "0D0A"
FEED_5 = "1B6405"  # ESC d 5  — feed 5 lines
CUT = "1B6D"  # ESC m     — partial cut
QR_SIZE = "1D010432"  # module size
QR_MODEL_PAYMENT = "1D010305"
QR_MODEL_CASH = "1D010308"
QR_STORE_PREFIX = "1D0101"

SEPARATOR_HEX = "2D" * 26  # 26 hyphens
LINE_WIDTH = 30

TYPE_QR_PAYMENT = "payment"
TYPE_QR_CASH = "cash"


def str_to_hex(text: str) -> str:
    """``strtoupper(bin2hex($str))`` — the PHP helper this is ported from."""
    return text.encode("utf-8").hex().upper()


def hex_to_bytes(hex_string: str) -> bytes:
    """Decode a ``uartData`` field back to raw printer bytes."""
    return bytes.fromhex(hex_string)


def wrap_center(text: str, width: int = LINE_WIDTH) -> str:
    """Centre-align and wrap text, one ESC/POS line per output line.

    Mirrors PHP ``wordwrap($text, $width, "\\n", false)``: long words are not
    broken, which is why a 30-character limit can still emit a longer line.
    """
    hex_parts: list[str] = []
    for paragraph in text.strip().split("\n"):
        lines = textwrap.wrap(
            paragraph, width=width, break_long_words=False, break_on_hyphens=False
        ) or [""]
        for line in lines:
            if not line.strip():
                continue
            hex_parts.append(ESC_ALIGN_CENTER + str_to_hex(line.strip()) + CRLF)
    return "".join(hex_parts)


def _uart_block(hex_data: str) -> dict[str, object]:
    """Wrap a hex payload the way the controller expects.

    ``uartDataLen`` is the length of the **hex string**, not the byte count.
    That is what ``strlen($header)`` computes in the PHP, and the hardware
    accepts it, so the quirk is preserved deliberately — halving it would be a
    silent protocol change.
    """
    return {"uartNo": 2, "uartDataLen": len(hex_data), "uartData": hex_data}


@dataclass(frozen=True)
class TicketHeader:
    store_name: str
    store_address: str
    qris: str
    type_qr: str = TYPE_QR_CASH


@dataclass(frozen=True)
class TicketBody:
    gate: str
    datetime: str
    trx: str
    vehicle: str | None
    lost_motor: float
    lost_car: float
    stay_motor: float
    stay_car: float
    police_number: str | None
    type_qr: str = TYPE_QR_CASH


def build_gate_in_1(header: TicketHeader) -> list[dict[str, object]]:
    """Part one of the entry ticket: site header plus the QR code.

    Port of ``TicketPrinterService::buildGateIn1``.
    """
    store = wrap_center(header.store_name)
    address = wrap_center(header.store_address)

    qr_length_hex = f"{len(header.qris):02X}"
    qr_hex = str_to_hex(header.qris)

    header_hex = store + address + CRLF + ESC_ALIGN_CENTER

    model = (
        QR_MODEL_PAYMENT if header.type_qr == TYPE_QR_PAYMENT else QR_MODEL_CASH
    )
    qr_block = model + QR_SIZE + QR_STORE_PREFIX + qr_length_hex + "00" + qr_hex

    return [_uart_block(header_hex), _uart_block(qr_block)]


def build_gate_in_2(body: TicketBody) -> list[dict[str, object]]:
    """Part two: plate, ticket number, entry time, and the penalty footer.

    Port of ``TicketPrinterService::buildGateIn2``. Published 200 ms after part
    one — see ``usleep(200000)`` in ``GateController::gatein()``.
    """
    scan_text = (
        "Scan QRIS untuk bayar non-tunai"
        if body.type_qr == TYPE_QR_PAYMENT
        else "Simpan QR untuk Keluar Parkir"
    )
    plate = body.police_number or "-"

    info_scan = "1D0102" + CRLF + ESC_ALIGN_CENTER + str_to_hex(scan_text)

    line = CRLF + ESC_ALIGN_CENTER + SEPARATOR_HEX + CRLF

    footer = (
        ESC_ALIGN_CENTER
        + str_to_hex(
            f"Tiket hilang motor Rp{_amount(body.lost_motor)} "
            f"mobil Rp{_amount(body.lost_car)}"
        )
        + CRLF
        + ESC_ALIGN_CENTER
        + str_to_hex(
            f"Denda inap motor Rp{_amount(body.stay_motor)} "
            f"mobil Rp{_amount(body.stay_car)}"
        )
        + FEED_5
        + CUT
    )

    uart_data = (
        CRLF + ESC_ALIGN_CENTER + str_to_hex(f"Plat: {plate}")
        + CRLF + ESC_ALIGN_CENTER + str_to_hex(
            f"{body.trx}-{body.vehicle}-IN {body.gate}"
        )
        + CRLF + ESC_ALIGN_CENTER + str_to_hex(f"Enter Time: {body.datetime}")
        + line
        + footer
    )

    return [_uart_block(info_scan), _uart_block(uart_data)]


def _amount(value: float) -> str:
    """Render a fee the way PHP string-concatenation does.

    ``parking_fees`` columns are doubles, so PHP prints ``10000`` for 10000.0.
    Python's default ``str(10000.0)`` would give ``10000.0`` and change the
    printed ticket.
    """
    if value is None:
        return "0"
    as_int = int(value)
    return str(as_int) if as_int == value else str(value)


# ---------------------------------------------------------------------------
# Decoding, for the mock printer and for tests
# ---------------------------------------------------------------------------


def render_ticket(uart_blocks: list[dict]) -> str:
    """Turn ``txUartData`` blocks back into readable text.

    Used by the mock gate controller to show what a driver would receive, and
    by the tests to compare against the real decoded ticket in flow.md §5.

    The blocks are joined before decoding, not decoded one by one: a line break
    can straddle two blocks (part two opens with the CRLF that terminates part
    one's last line), and decoding separately would swallow it.
    """
    raw = b"".join(
        hex_to_bytes(str(block.get("uartData", ""))) for block in uart_blocks
    )
    return _strip_control_codes(raw)


def _strip_control_codes(raw: bytes) -> str:
    """Drop ESC/POS control sequences, keep the printable text.

    Deliberately simple: it recognises the handful of sequences this project
    emits rather than implementing the full ESC/POS grammar.
    """
    text: list[str] = []
    i = 0
    while i < len(raw):
        byte = raw[i]

        if byte == 0x1B:  # ESC — two- or three-byte sequences
            if i + 1 < len(raw) and raw[i + 1] in (0x61, 0x64):
                i += 3
                continue
            i += 2
            continue

        if byte == 0x1D:  # GS — QR store command carries a payload
            if raw[i : i + 3] == b"\x1d\x01\x01" and i + 4 < len(raw):
                length = raw[i + 3]
                payload = raw[i + 5 : i + 5 + length].decode("utf-8", "replace")
                text.append(f"[QR] {payload}\n")
                i += 5 + length
                continue
            i += 4
            continue

        if byte in (0x0D, 0x0A):
            if text and not text[-1].endswith("\n"):
                text.append("\n")
            i += 1
            continue

        text.append(chr(byte))
        i += 1

    return "".join(text)
