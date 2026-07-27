"""The reference for these tests is the real ticket decoded off the wire at the
Salatiga site (flow.md §5, ticket 9922432407). If a change here breaks them,
the thermal printer output has changed."""

import pytest

from trafix.escpos import (
    CRLF,
    ESC_ALIGN_CENTER,
    QR_MODEL_CASH,
    QR_MODEL_PAYMENT,
    TYPE_QR_CASH,
    TYPE_QR_PAYMENT,
    TicketBody,
    TicketHeader,
    build_gate_in_1,
    build_gate_in_2,
    hex_to_bytes,
    render_ticket,
    str_to_hex,
    wrap_center,
)

SITE_NAME = "Sinode GKJ"
SITE_ADDRESS = "Jl. Dr. Sumardi No. 8, Kec. Sidorejo, Kota Salatiga, Jawa Tengah"

HEADER = TicketHeader(
    store_name=SITE_NAME,
    store_address=SITE_ADDRESS,
    qris="9922432407",
    type_qr=TYPE_QR_CASH,
)

BODY = TicketBody(
    gate="1",
    datetime="2026-07-24 03:52:02",
    trx="9922432407",
    vehicle="Motor",
    lost_motor=10000.0,
    lost_car=30000.0,
    stay_motor=10000.0,
    stay_car=25000.0,
    police_number="H488AI",
    type_qr=TYPE_QR_CASH,
)

# Exactly as printed on site. No trailing newline: the last text line is
# followed by the feed and cut commands, not a CRLF.
EXPECTED_TICKET = """\
Sinode GKJ
Jl. Dr. Sumardi No. 8, Kec.
Sidorejo, Kota Salatiga, Jawa
Tengah
[QR] 9922432407
Simpan QR untuk Keluar Parkir
Plat: H488AI
9922432407-Motor-IN 1
Enter Time: 2026-07-24 03:52:02
--------------------------
Tiket hilang motor Rp10000 mobil Rp30000
Denda inap motor Rp10000 mobil Rp25000"""


def test_ticket_matches_the_one_captured_on_site():
    rendered = render_ticket(build_gate_in_1(HEADER) + build_gate_in_2(BODY))
    assert rendered == EXPECTED_TICKET


def test_address_wraps_exactly_as_php_wordwrap_did():
    """The observed break points are 27 / 29 / 6 characters."""
    lines = render_ticket(build_gate_in_1(HEADER)).split("\n")
    assert lines[1] == "Jl. Dr. Sumardi No. 8, Kec."
    assert lines[2] == "Sidorejo, Kota Salatiga, Jawa"
    assert lines[3] == "Tengah"


def test_amounts_print_as_integers_not_floats():
    """parking_fees columns are doubles; PHP prints 10000, not 10000.0."""
    rendered = render_ticket(build_gate_in_2(BODY))
    assert "Rp10000 " in rendered
    assert "10000.0" not in rendered


# -- structure of the wire blocks -------------------------------------------


def test_gate_in_1_emits_two_uart_blocks():
    blocks = build_gate_in_1(HEADER)
    assert len(blocks) == 2
    for block in blocks:
        assert block["uartNo"] == 2


def test_uart_data_len_counts_hex_characters_not_bytes():
    """Reproduces PHP strlen() on the hex string.

    It is arguably wrong, but the hardware accepts it and halving it would be
    a silent protocol change.
    """
    for block in build_gate_in_1(HEADER) + build_gate_in_2(BODY):
        assert block["uartDataLen"] == len(block["uartData"])
        assert block["uartDataLen"] == 2 * len(hex_to_bytes(block["uartData"]))


def test_uart_data_is_valid_hex():
    for block in build_gate_in_1(HEADER) + build_gate_in_2(BODY):
        hex_to_bytes(block["uartData"])  # raises if malformed


def test_qr_length_byte_is_two_hex_digits():
    qr_block = build_gate_in_1(HEADER)[1]["uartData"]
    # 1D0101 <len> 00 <payload>
    assert qr_block.startswith(QR_MODEL_CASH)
    index = qr_block.index("1D0101") + len("1D0101")
    assert qr_block[index : index + 2] == "0A"  # len("9922432407") == 10


def test_long_qr_payload_still_gets_a_two_digit_length():
    header = TicketHeader(SITE_NAME, SITE_ADDRESS, "X" * 200, TYPE_QR_CASH)
    qr_block = build_gate_in_1(header)[1]["uartData"]
    index = qr_block.index("1D0101") + len("1D0101")
    assert qr_block[index : index + 2] == "C8"  # 200


# -- the payment vs cash branch ---------------------------------------------


def test_cash_and_payment_use_different_qr_models():
    cash = build_gate_in_1(HEADER)[1]["uartData"]
    payment = build_gate_in_1(
        TicketHeader(SITE_NAME, SITE_ADDRESS, "9922432407", TYPE_QR_PAYMENT)
    )[1]["uartData"]
    assert cash.startswith(QR_MODEL_CASH)
    assert payment.startswith(QR_MODEL_PAYMENT)


def test_payment_ticket_tells_the_driver_to_scan_for_qris():
    body = TicketBody(**{**BODY.__dict__, "type_qr": TYPE_QR_PAYMENT})
    rendered = render_ticket(build_gate_in_2(body))
    assert "Scan QRIS untuk bayar non-tunai" in rendered
    assert "Simpan QR untuk Keluar Parkir" not in rendered


# -- the blank-plate case, which is 4 of 6 tickets on site (§7.7) ------------


def test_missing_plate_prints_a_dash():
    body = TicketBody(**{**BODY.__dict__, "police_number": None})
    assert "Plat: -" in render_ticket(build_gate_in_2(body))


@pytest.mark.parametrize("plate", ["", None])
def test_empty_plate_never_prints_none(plate):
    body = TicketBody(**{**BODY.__dict__, "police_number": plate})
    rendered = render_ticket(build_gate_in_2(body))
    assert "None" not in rendered


# -- helpers ----------------------------------------------------------------


def test_str_to_hex_is_uppercase():
    assert str_to_hex("AB") == "4142"
    assert str_to_hex("Sinode GKJ").isupper()


def test_wrap_center_emits_one_aligned_line_per_wrapped_line():
    hex_out = wrap_center("one two three", width=7)
    assert hex_out.count(ESC_ALIGN_CENTER) == 2
    assert hex_out.count(CRLF) == 2


def test_wrap_center_skips_blank_lines():
    assert wrap_center("   ") == ""
