import pytest

from tests.conftest import FakeCamera, evt, make_config
from trafix.db import (
    STATUS_ACTIVE,
    STATUS_AWAITING_PAYMENT,
    STATUS_CLOSED,
    STATUS_PENDING,
)
from trafix.envelope import (
    CMD_CHECKOUT_RESULT,
    CMD_OPEN_GATE,
    EVT_CHECKOUT_REQUEST,
    EVT_PAYMENT_SETTLED,
    EVT_TICKET_PRINTED,
    EVT_TICKET_REQUEST,
    EVT_VEHICLE_PASSED,
)
from trafix.lpr_client import REASON_NO_PLATE, PlateRead
from trafix.server.checkin import CheckinService
from trafix.server.checkout import (
    ERR_ALREADY_CLOSED,
    ERR_NOT_ACTIVE,
    ERR_NO_MATCHING_ENTRY,
    ERR_PLATE_MISMATCH,
    ERR_UNKNOWN_TICKET,
    CheckoutService,
)
from trafix.tickets import PLATE_UNKNOWN

PLATE = "B1234XYZ"
OTHER_PLATE = "D5678ABC"


class Lanes:
    """An entry and an exit lane sharing one database and clock."""

    def __init__(self, config, db, clock, entry_camera, exit_camera, publisher):
        self.db = db
        self.clock = clock
        self.entry_camera = entry_camera
        self.exit_camera = exit_camera
        self.publisher = publisher
        self.checkin = CheckinService(
            lane="in", config=config, db=db, lpr=entry_camera,
            publish=publisher, clock=clock,
        )
        self.checkout = CheckoutService(
            lane="out", config=config, db=db, lpr=exit_camera,
            publish=publisher, clock=clock,
        )

    def park_a_car(self, plate: str = PLATE):
        """Run a complete entry and return the ACTIVE transaction."""
        self.entry_camera.default = PlateRead(
            ok=True, plate=plate, confidence=0.95, image_url="http://cam/in.jpg"
        )
        transaction = self.checkin.on_ticket_request(evt(EVT_TICKET_REQUEST))
        self.checkin.on_ticket_printed(
            evt(EVT_TICKET_PRINTED, barcode=transaction.barcode)
        )
        self.checkin.on_vehicle_passed(
            evt(EVT_VEHICLE_PASSED, barcode=transaction.barcode)
        )
        self.publisher.clear()
        return self.db.get(transaction.id)


def build(config, db, clock, publisher) -> Lanes:
    return Lanes(config, db, clock, FakeCamera(), FakeCamera(), publisher)


@pytest.fixture
def lanes(config, db, clock, publisher):
    return build(config, db, clock, publisher)


def exit_read(plate: str | None = PLATE, ok: bool = True):
    return PlateRead(
        ok=ok, plate=plate, confidence=0.91 if ok else None,
        image_url="http://cam/out.jpg", reason=None if ok else REASON_NO_PLATE,
    )


# -- the happy path ---------------------------------------------------------


def test_matching_plate_quotes_a_fee(lanes):
    transaction = lanes.park_a_car()
    lanes.clock.advance(hours=2, minutes=5)
    lanes.exit_camera.default = exit_read(PLATE)

    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, device_id="manless_out", barcode=transaction.barcode)
    )

    assert result.status == STATUS_AWAITING_PAYMENT
    assert result.plate_match is True
    assert result.duration_minutes == 125
    assert result.fee == 7000  # first hour 3000 + 2 started hours x 2000

    command = lanes.publisher.last(CMD_CHECKOUT_RESULT)
    assert command.get("plate_in") == PLATE
    assert command.get("plate_out") == PLATE
    assert command.get("fee") == 7000
    assert command.get("error") is None


def test_payment_then_pass_closes_the_transaction(lanes, db):
    transaction = lanes.park_a_car()
    lanes.clock.advance(hours=1)
    lanes.exit_camera.default = exit_read(PLATE)
    lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )

    lanes.checkout.on_payment_settled(
        evt(EVT_PAYMENT_SETTLED, barcode=transaction.barcode, amount=5000)
    )
    command = lanes.publisher.last(CMD_OPEN_GATE)
    assert command.get("direction") == "exit"
    assert db.get(transaction.id).paid is True

    lanes.checkout.on_vehicle_passed(
        evt(EVT_VEHICLE_PASSED, barcode=transaction.barcode)
    )
    assert db.get(transaction.id).status == STATUS_CLOSED


def test_leaving_within_grace_is_free(lanes):
    transaction = lanes.park_a_car()
    lanes.clock.advance(minutes=5)
    lanes.exit_camera.default = exit_read(PLATE)

    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )
    assert result.fee == 0


def test_exit_snapshot_is_recorded(lanes):
    transaction = lanes.park_a_car()
    lanes.clock.advance(hours=1)
    lanes.exit_camera.default = exit_read(PLATE)
    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )
    assert result.entry_image_url == "http://cam/in.jpg"
    assert result.exit_image_url == "http://cam/out.jpg"


# -- plate mismatch policy --------------------------------------------------


def test_flag_policy_opens_the_gate_but_marks_the_record(db, clock, publisher):
    lanes = build(make_config(plate_mismatch="flag"), db, clock, publisher)
    transaction = lanes.park_a_car(PLATE)
    lanes.clock.advance(hours=1)
    lanes.exit_camera.default = exit_read(OTHER_PLATE)

    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )

    assert result.status == STATUS_AWAITING_PAYMENT
    assert result.plate_match is False
    assert result.flagged is True
    assert result.flag_reason == "plate_mismatch"
    assert lanes.publisher.last(CMD_CHECKOUT_RESULT).get("error") is None


def test_deny_policy_keeps_the_gate_shut(db, clock, publisher):
    lanes = build(make_config(plate_mismatch="deny"), db, clock, publisher)
    transaction = lanes.park_a_car(PLATE)
    lanes.clock.advance(hours=1)
    lanes.exit_camera.default = exit_read(OTHER_PLATE)

    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )

    assert result is None
    assert lanes.publisher.last(CMD_CHECKOUT_RESULT).get("error") == ERR_PLATE_MISMATCH
    assert db.get(transaction.id).status == STATUS_ACTIVE
    assert db.get(transaction.id).flagged is True


def test_allow_policy_ignores_the_mismatch(db, clock, publisher):
    lanes = build(make_config(plate_mismatch="allow"), db, clock, publisher)
    transaction = lanes.park_a_car(PLATE)
    lanes.clock.advance(hours=1)
    lanes.exit_camera.default = exit_read(OTHER_PLATE)

    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )
    assert result.status == STATUS_AWAITING_PAYMENT
    assert result.flagged is False


def test_plate_formatting_difference_is_not_a_mismatch(lanes):
    transaction = lanes.park_a_car("B1234XYZ")
    lanes.clock.advance(hours=1)
    lanes.exit_camera.default = exit_read("b 1234 xyz")

    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )
    assert result.plate_match is True
    assert result.flagged is False


@pytest.mark.parametrize("policy", ["allow", "flag", "deny"])
def test_unread_exit_plate_is_not_treated_as_a_mismatch(db, clock, publisher, policy):
    """An unreadable exit camera must not strand a driver who has a valid ticket."""
    lanes = build(make_config(plate_mismatch=policy), db, clock, publisher)
    transaction = lanes.park_a_car(PLATE)
    lanes.clock.advance(hours=1)
    lanes.exit_camera.default = exit_read(None, ok=False)

    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )

    assert result is not None
    assert result.plate_match is None
    assert result.exit_plate == PLATE_UNKNOWN


def test_unknown_entry_plate_is_not_a_mismatch(db, clock, publisher):
    """A car that entered while the camera was down must still be able to leave."""
    lanes = build(make_config(plate_mismatch="deny"), db, clock, publisher)
    lanes.entry_camera.queue(PlateRead(ok=False, reason=REASON_NO_PLATE))
    transaction = lanes.checkin.on_ticket_request(evt(EVT_TICKET_REQUEST))
    lanes.checkin.on_vehicle_passed(evt(EVT_VEHICLE_PASSED, barcode=transaction.barcode))
    lanes.publisher.clear()

    lanes.clock.advance(hours=1)
    lanes.exit_camera.default = exit_read(PLATE)
    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )

    assert result is not None
    assert result.plate_match is None


# -- refusals ---------------------------------------------------------------


def test_unknown_barcode_is_refused(lanes, db):
    lanes.exit_camera.default = exit_read(PLATE)
    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode="999999999999")
    )
    assert result is None
    command = lanes.publisher.last(CMD_CHECKOUT_RESULT)
    assert command.get("error") == ERR_UNKNOWN_TICKET
    assert command.get("text")


def test_reused_ticket_is_refused(lanes, db):
    transaction = lanes.park_a_car()
    lanes.clock.advance(hours=1)
    lanes.exit_camera.default = exit_read(PLATE)
    lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )
    lanes.checkout.on_payment_settled(
        evt(EVT_PAYMENT_SETTLED, barcode=transaction.barcode)
    )
    lanes.checkout.on_vehicle_passed(
        evt(EVT_VEHICLE_PASSED, barcode=transaction.barcode)
    )
    lanes.publisher.clear()

    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )
    assert result is None
    assert lanes.publisher.last(CMD_CHECKOUT_RESULT).get("error") == ERR_ALREADY_CLOSED


def test_ticket_whose_car_never_entered_is_refused(lanes, db):
    """PENDING means the barrier never confirmed the car came in."""
    transaction = lanes.checkin.on_ticket_request(evt(EVT_TICKET_REQUEST))
    assert db.get(transaction.id).status == STATUS_PENDING
    lanes.publisher.clear()

    lanes.clock.advance(hours=1)
    lanes.exit_camera.default = exit_read(PLATE)
    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )

    assert result is None
    assert lanes.publisher.last(CMD_CHECKOUT_RESULT).get("error") == ERR_NOT_ACTIVE


def test_payment_for_a_ticket_that_was_never_quoted_is_ignored(lanes, db):
    transaction = lanes.park_a_car()
    lanes.checkout.on_payment_settled(
        evt(EVT_PAYMENT_SETTLED, barcode=transaction.barcode)
    )
    assert not lanes.publisher.has(CMD_OPEN_GATE)
    assert db.get(transaction.id).paid is False


def test_payment_for_unknown_ticket_is_ignored(lanes):
    lanes.checkout.on_payment_settled(
        evt(EVT_PAYMENT_SETTLED, barcode="999999999999")
    )
    assert lanes.publisher.sent == []


# -- lost ticket ------------------------------------------------------------


def test_lost_ticket_resolves_by_plate_and_charges_the_flat_fee(lanes):
    transaction = lanes.park_a_car(PLATE)
    lanes.clock.advance(minutes=20)
    lanes.exit_camera.default = exit_read(PLATE)

    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode="", lost_ticket=True)
    )

    assert result.id == transaction.id
    assert result.fee == 50000


def test_lost_ticket_with_no_matching_car_is_refused(lanes):
    lanes.exit_camera.default = exit_read(OTHER_PLATE)
    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode="", lost_ticket=True)
    )
    assert result is None
    assert (
        lanes.publisher.last(CMD_CHECKOUT_RESULT).get("error") == ERR_NO_MATCHING_ENTRY
    )


def test_lost_ticket_needs_a_readable_plate(lanes):
    lanes.park_a_car(PLATE)
    lanes.exit_camera.default = exit_read(None, ok=False)
    result = lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode="", lost_ticket=True)
    )
    assert result is None


# -- audit trail ------------------------------------------------------------


def test_checkout_is_recorded_for_the_audit_trail(lanes, db):
    transaction = lanes.park_a_car()
    lanes.clock.advance(hours=3)
    lanes.exit_camera.default = exit_read(PLATE)
    lanes.checkout.on_checkout_request(
        evt(EVT_CHECKOUT_REQUEST, barcode=transaction.barcode)
    )
    lanes.checkout.on_payment_settled(
        evt(EVT_PAYMENT_SETTLED, barcode=transaction.barcode)
    )
    lanes.checkout.on_vehicle_passed(
        evt(EVT_VEHICLE_PASSED, barcode=transaction.barcode)
    )

    types = [e["type"] for e in db.list_events(transaction_id=transaction.id)]
    for expected in ("fee_quoted", "payment_settled", "exit_completed"):
        assert expected in types
