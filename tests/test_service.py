"""Check-in and check-out at the service layer: database, no HTTP, no broker.

Several tests here pin behaviour that the production system gets wrong. Those
are labelled — they are the reason this port exists.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from trafix.escpos import render_ticket
from trafix.models import (
    GATE_STATUS_IN,
    GATE_STATUS_OUT,
    PAYMENT_PAID,
    Members,
    Transactions,
    XenditQrPool,
)
from trafix.service import (
    STATUS_MEMBER_EXPIRED,
    STATUS_NOT_FOUND,
    STATUS_PLATE_MISMATCH,
    STATUS_SUCCESS,
    STATUS_SUCCESS_MEMBER,
    STATUS_SUCCESS_TICKET,
    STATUS_TICKET_USED,
    normalize_plate,
)

PLATE = "H488AI"


def check_in(service, *, plate=PLATE, vehicle_id=1, url=None):
    return service.gate_in(
        gate="1",
        vehicle_id=vehicle_id,
        plate_num=plate,
        url_gambar=url,
        serial_no="441D6491AF17",
    )


# -- check-in ---------------------------------------------------------------


def test_check_in_creates_a_transaction(service, session_factory):
    result = check_in(service)

    assert result.status == STATUS_SUCCESS
    assert len(result.transaction_code) == 10

    with session_factory() as session:
        row = session.scalar(
            select(Transactions).where(
                Transactions.transaction_code == result.transaction_code
            )
        )
    assert row.police_number == PLATE
    assert row.gate_in == "1"
    assert row.gate_status == GATE_STATUS_IN
    assert row.time_checkout is None
    assert row.is_inside()


def test_check_in_prints_two_parts_two_hundred_ms_apart(service, publisher):
    """The controller drops the second half if they arrive back to back."""
    check_in(service)
    assert len(publisher.printed) == 2
    assert all(gate == "1" for gate, _ in publisher.printed)


def test_the_printed_ticket_is_the_real_layout(service, publisher):
    result = check_in(service)
    ticket = render_ticket(publisher.printed[0][1] + publisher.printed[1][1])

    assert "Sinode GKJ" in ticket
    assert f"[QR] {result.transaction_code}" in ticket
    assert f"Plat: {PLATE}" in ticket
    assert f"{result.transaction_code}-Motor-IN 1" in ticket
    assert "Tiket hilang motor Rp10000 mobil Rp30000" in ticket


def test_transaction_codes_are_unique(service):
    codes = {check_in(service).transaction_code for _ in range(25)}
    assert len(codes) == 25


def test_plate_is_normalised_on_the_way_in(service, session_factory):
    result = check_in(service, plate="h 488-ai")
    with session_factory() as session:
        row = session.scalar(
            select(Transactions).where(
                Transactions.transaction_code == result.transaction_code
            )
        )
    assert row.police_number == "H488AI"


def test_a_blank_plate_is_still_a_valid_ticket(service, publisher):
    """4 of 6 tickets on site recorded no plate (§7.7). The gate still works."""
    result = check_in(service, plate=None)
    assert result.status == STATUS_SUCCESS
    assert result.plate is None
    assert "Plat: -" in render_ticket(publisher.printed[1][1])


def test_cash_site_puts_the_ticket_code_in_the_qr(service):
    result = check_in(service)
    assert result.type_qr == "cash"


def test_qris_site_draws_from_the_pool(service, session_factory, qris_site):
    with session_factory() as session:
        session.add(
            XenditQrPool(
                vehicle_id=1,
                qr_string="00020101021226680016COM.XENDIT",
                pr_id="pr-1",
                status="available",
                expired_at=datetime.now() + timedelta(hours=2),
            )
        )
        session.commit()

    result = check_in(service)
    assert result.type_qr == "payment"

    with session_factory() as session:
        qr = session.scalar(select(XenditQrPool))
        assert qr.status == "used"


def test_empty_qris_pool_falls_back_to_cash(service, qris_site):
    """On site this happens whenever the queue worker is not running (§2)."""
    result = check_in(service)
    assert result.type_qr == "cash"


# -- check-out --------------------------------------------------------------


def test_check_out_by_ticket_settles_and_opens_the_barrier(
    service, publisher, clock, session_factory
):
    entry = check_in(service)
    clock.advance(hours=2)

    result = service.gate_out(gate="2", code=entry.transaction_code, plate_num=PLATE)

    assert result.status == STATUS_SUCCESS_TICKET
    assert result.total > 0
    assert result.plate_match is True
    assert publisher.barriers == [("2", True)], "the exit barrier must be commanded"

    with session_factory() as session:
        row = session.scalar(
            select(Transactions).where(
                Transactions.transaction_code == entry.transaction_code
            )
        )
    assert row.time_checkout is not None
    assert row.payment_status == PAYMENT_PAID
    assert row.gate_status == GATE_STATUS_OUT
    assert not row.is_inside()


def test_exit_barrier_is_commanded_at_all(service, publisher, clock):
    """Fix for §7.6: production sends no exit command, ever."""
    entry = check_in(service)
    clock.advance(minutes=30)
    service.gate_out(gate="2", code=entry.transaction_code)
    assert publisher.barriers, "production opens no exit barrier; this port must"


def test_check_out_within_grace_costs_nothing(service, clock):
    entry = check_in(service)
    clock.advance(minutes=5)
    result = service.gate_out(gate="2", code=entry.transaction_code)
    assert result.total == 0


def test_a_used_ticket_is_refused(service, clock):
    entry = check_in(service)
    clock.advance(hours=1)
    service.gate_out(gate="2", code=entry.transaction_code)

    again = service.gate_out(gate="2", code=entry.transaction_code)
    assert again.status == STATUS_TICKET_USED


def test_a_used_ticket_does_not_open_the_barrier_again(service, publisher, clock):
    entry = check_in(service)
    clock.advance(hours=1)
    service.gate_out(gate="2", code=entry.transaction_code)
    publisher.barriers.clear()

    service.gate_out(gate="2", code=entry.transaction_code)
    assert publisher.barriers == []


def test_unknown_ticket_is_refused(service, publisher):
    result = service.gate_out(gate="2", code="0000000000")
    assert result.status == STATUS_NOT_FOUND
    assert publisher.barriers == []


def test_check_out_by_plate_when_the_ticket_is_unreadable(service, clock):
    """The plate is advisory, but it is better than nothing."""
    check_in(service, plate=PLATE)
    clock.advance(hours=1)

    result = service.gate_out(gate="2", code=None, plate_num=PLATE)
    assert result.status == STATUS_SUCCESS_TICKET


def test_ticket_code_wins_over_a_disagreeing_plate(service, clock):
    """§7.7: the two cameras produce different strings for the same car.

    The ticket must still work.
    """
    entry = check_in(service, plate="H488AI")
    clock.advance(hours=1)

    result = service.gate_out(gate="2", code=entry.transaction_code, plate_num="H4818AI")

    assert result.status == STATUS_SUCCESS_TICKET
    assert result.plate_match is False, "the mismatch is recorded..."
    assert result.total > 0, "...but it does not block the exit"


def test_a_mismatch_is_written_to_the_record_for_review(service, clock, session_factory):
    entry = check_in(service, plate="H488AI")
    clock.advance(hours=1)
    service.gate_out(gate="2", code=entry.transaction_code, plate_num="D9999ZZ")

    with session_factory() as session:
        row = session.scalar(
            select(Transactions).where(
                Transactions.transaction_code == entry.transaction_code
            )
        )
    assert "mismatch" in (row.keterangan or "")


def test_the_exit_read_never_overwrites_a_known_entry_plate(
    service, clock, session_factory
):
    entry = check_in(service, plate="H488AI")
    clock.advance(hours=1)
    service.gate_out(gate="2", code=entry.transaction_code, plate_num="H4818AI")

    with session_factory() as session:
        row = session.scalar(
            select(Transactions).where(
                Transactions.transaction_code == entry.transaction_code
            )
        )
    assert row.police_number == "H488AI", "the ticket was issued against this plate"


def test_the_exit_read_fills_in_a_missing_entry_plate(service, clock, session_factory):
    entry = check_in(service, plate=None)
    clock.advance(hours=1)
    service.gate_out(gate="2", code=entry.transaction_code, plate_num="H488AI")

    with session_factory() as session:
        row = session.scalar(
            select(Transactions).where(
                Transactions.transaction_code == entry.transaction_code
            )
        )
    assert row.police_number == "H488AI"


def test_an_unread_plate_is_not_a_mismatch(service, clock):
    entry = check_in(service, plate="H488AI")
    clock.advance(hours=1)
    result = service.gate_out(gate="2", code=entry.transaction_code, plate_num=None)
    assert result.plate_match is None


# -- strict plate matching, off by default ----------------------------------


def test_strict_mode_refuses_a_mismatch(session_factory, publisher, clock):
    from types import SimpleNamespace

    from trafix.service import ParkingService

    config = SimpleNamespace(
        policies=SimpleNamespace(require_plate_match=True, command_exit_barrier=True)
    )
    service = ParkingService(
        session_factory, publisher=publisher, clock=clock, config=config
    )
    entry = check_in(service, plate="H488AI")
    clock.advance(hours=1)

    result = service.gate_out(gate="2", code=entry.transaction_code, plate_num="D9999ZZ")

    assert result.status == STATUS_PLATE_MISMATCH
    assert publisher.barriers == []


# -- members ----------------------------------------------------------------


def test_a_member_pays_nothing(service, clock, session_factory):
    with session_factory() as session:
        member = session.scalar(select(Members))
        plate = member.police_number
        member.vehicle_id = 1
        session.commit()

    entry = check_in(service, plate=plate, vehicle_id=1)
    clock.advance(hours=6)
    result = service.gate_out(gate="2", code=entry.transaction_code, plate_num=plate)

    assert result.status == STATUS_SUCCESS_MEMBER
    assert result.total == 0
    assert result.member_name == "Angelo"


def test_member_card_entry_creates_a_paid_no_print_transaction(
    service, session_factory, publisher
):
    result = service.member_gate_in(
        gate="1", card_no="006343040", serial_no="441D6491AF17", vehicle_id=1
    )

    assert result.status == STATUS_SUCCESS
    assert result.member_name == "Angelo"
    assert result.plate == "H4818AI"
    assert publisher.printed == []  # members get no paper ticket

    with session_factory() as session:
        row = session.scalar(
            select(Transactions).where(
                Transactions.transaction_code == result.transaction_code
            )
        )
        assert row.card_number == "006343040"
        assert row.police_number == "H4818AI"
        assert row.type == "member"
        assert row.payment_status == PAYMENT_PAID
        assert row.total == 0
        assert row.status == "gatein"
        assert row.gate_status == GATE_STATUS_IN
        assert row.gate_in == "1"


def test_member_card_entry_rejects_unknown_card(service):
    result = service.member_gate_in(
        gate="1", card_no="999999999", serial_no="x", vehicle_id=1
    )
    assert result.status == STATUS_NOT_FOUND
    assert result.transaction_code is None


def test_member_card_entry_rejects_an_expired_subscription(service, session_factory):
    with session_factory() as session:
        member = session.scalar(select(Members))
        member.time_limit = datetime.today().date() - timedelta(days=1)
        session.commit()

    result = service.member_gate_in(
        gate="1", card_no="006343040", serial_no="x", vehicle_id=1
    )
    assert result.status == STATUS_MEMBER_EXPIRED


def test_member_card_entry_rejects_empty_card(service):
    result = service.member_gate_in(gate="1", card_no="", serial_no="x")
    assert result.status == STATUS_NOT_FOUND


# -- quoting ----------------------------------------------------------------


def test_quote_does_not_settle_anything(service, clock, session_factory, publisher):
    entry = check_in(service)
    clock.advance(hours=2)

    quote = service.quote(code=entry.transaction_code)
    assert quote.total > 0

    with session_factory() as session:
        row = session.scalar(
            select(Transactions).where(
                Transactions.transaction_code == entry.transaction_code
            )
        )
    assert row.time_checkout is None, "quoting must not check the car out"
    assert publisher.barriers == []


def test_quote_for_an_unknown_ticket(service):
    assert service.quote(code="nope").status == STATUS_NOT_FOUND


def test_lost_ticket_charges_the_penalty(service, clock):
    check_in(service, plate=PLATE)
    clock.advance(minutes=20)
    result = service.gate_out(gate="2", plate_num=PLATE, lost=True)
    # Motor: ticket_charge 10000 + one flat period 2000
    assert result.total == 12000


# -- lookup helpers ---------------------------------------------------------


def test_qris_ticket_is_found_by_its_qr_string(service, session_factory, qris_site):
    with session_factory() as session:
        session.add(
            XenditQrPool(
                vehicle_id=1,
                qr_string="QRIS-PAYLOAD-123",
                pr_id="pr-9",
                status="available",
                expired_at=datetime.now() + timedelta(hours=2),
            )
        )
        session.commit()

    check_in(service)
    # The driver's ticket shows the QR payload, not the transaction code.
    result = service.quote(code="QRIS-PAYLOAD-123")
    assert result.status in (STATUS_SUCCESS, STATUS_SUCCESS_MEMBER)


@pytest.mark.parametrize(
    "raw,expected",
    [("h 488 ai", "H488AI"), ("H-488-AI", "H488AI"), ("", None), (None, None)],
)
def test_normalize_plate(raw, expected):
    assert normalize_plate(raw) == expected
