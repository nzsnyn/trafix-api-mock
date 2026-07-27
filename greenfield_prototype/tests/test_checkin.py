import pytest

from tests.conftest import FakeCamera, evt, make_config
from trafix.db import STATUS_ACTIVE, STATUS_PENDING
from trafix.envelope import (
    CMD_OPEN_GATE,
    CMD_PRINT_TICKET,
    CMD_REJECT,
    EVT_TICKET_PRINTED,
    EVT_TICKET_REQUEST,
    EVT_VEHICLE_PASSED,
)
from trafix.lpr_client import (
    REASON_LOW_CONFIDENCE,
    REASON_NO_PLATE,
    REASON_UNREACHABLE,
    PlateRead,
)
from trafix.server.checkin import CheckinService
from trafix.tickets import PLATE_UNKNOWN


def make_service(config, db, camera, publisher, clock) -> CheckinService:
    return CheckinService(
        lane="in", config=config, db=db, lpr=camera, publish=publisher, clock=clock
    )


@pytest.fixture
def service(config, db, camera, publisher, clock):
    return make_service(config, db, camera, publisher, clock)


# -- the happy path ---------------------------------------------------------


def test_button_press_issues_a_ticket(service, publisher, db):
    transaction = service.on_ticket_request(evt(EVT_TICKET_REQUEST))

    assert transaction is not None
    assert transaction.status == STATUS_PENDING
    assert transaction.entry_plate == "B1234XYZ"
    assert transaction.ticket_no.startswith("IN-")
    assert db.get_by_barcode(transaction.barcode) is not None

    command = publisher.last(CMD_PRINT_TICKET)
    assert command.get("ticket_no") == transaction.ticket_no
    assert command.get("barcode") == transaction.barcode
    assert command.get("plate") == "B1234XYZ"
    assert command.get("image_url") == "http://cam/images/x.jpg"


def test_print_command_correlates_to_the_button_press(service, publisher):
    request = evt(EVT_TICKET_REQUEST)
    service.on_ticket_request(request)
    assert publisher.last(CMD_PRINT_TICKET).correlation_id == request.msg_id


def test_ticket_printed_opens_the_barrier(service, publisher):
    transaction = service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    publisher.clear()

    service.on_ticket_printed(evt(EVT_TICKET_PRINTED, barcode=transaction.barcode))

    command = publisher.last(CMD_OPEN_GATE)
    assert command.get("ticket_no") == transaction.ticket_no
    assert command.get("direction") == "entry"


def test_vehicle_passed_activates_the_transaction(service, db):
    transaction = service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    service.on_ticket_printed(evt(EVT_TICKET_PRINTED, barcode=transaction.barcode))
    service.on_vehicle_passed(evt(EVT_VEHICLE_PASSED, barcode=transaction.barcode))

    assert db.get(transaction.id).status == STATUS_ACTIVE


def test_full_entry_sequence_emits_print_then_open(service, publisher):
    transaction = service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    service.on_ticket_printed(evt(EVT_TICKET_PRINTED, barcode=transaction.barcode))
    assert publisher.types == [CMD_PRINT_TICKET, CMD_OPEN_GATE]


def test_camera_is_asked_for_the_right_lane(service, camera):
    service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    assert camera.calls == [{"trigger": "button", "lane": "in"}]


# -- the LPR failure policy -------------------------------------------------

UNREADABLE = [
    PlateRead(ok=False, reason=REASON_NO_PLATE, detail="nothing seen"),
    PlateRead(ok=False, reason=REASON_UNREACHABLE, detail="connect refused"),
    PlateRead(ok=False, reason=REASON_LOW_CONFIDENCE, plate="B1", confidence=0.2),
]


@pytest.mark.parametrize("read", UNREADABLE)
def test_allow_policy_still_issues_a_ticket(db, publisher, clock, read):
    config = make_config(lpr_failure="allow")
    camera = FakeCamera()
    camera.queue(read)
    service = make_service(config, db, camera, publisher, clock)

    transaction = service.on_ticket_request(evt(EVT_TICKET_REQUEST))

    assert transaction is not None, "a car must never be trapped at the entry"
    assert transaction.entry_plate == PLATE_UNKNOWN
    assert transaction.flagged is True
    assert read.reason in transaction.flag_reason
    assert publisher.has(CMD_PRINT_TICKET)


@pytest.mark.parametrize("read", UNREADABLE)
def test_deny_policy_refuses_entry(db, publisher, clock, read):
    config = make_config(lpr_failure="deny")
    camera = FakeCamera()
    camera.queue(read)
    service = make_service(config, db, camera, publisher, clock)

    transaction = service.on_ticket_request(evt(EVT_TICKET_REQUEST))

    assert transaction is None
    assert not publisher.has(CMD_PRINT_TICKET)
    assert publisher.last(CMD_REJECT).get("reason") == "plate_unreadable"
    assert db.list_transactions() == []


def test_low_confidence_read_does_not_leak_its_plate(db, publisher, clock):
    """A read the camera itself doubts must not become the entry plate."""
    camera = FakeCamera()
    camera.queue(PlateRead(ok=False, reason=REASON_LOW_CONFIDENCE,
                           plate="B9999ZZ", confidence=0.3))
    service = make_service(make_config(), db, camera, publisher, clock)

    transaction = service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    assert transaction.entry_plate == PLATE_UNKNOWN


# -- button debounce --------------------------------------------------------


def test_repeat_press_reissues_the_same_ticket(service, publisher, clock, db):
    first = service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    publisher.clear()

    clock.advance(seconds=2)
    second = service.on_ticket_request(evt(EVT_TICKET_REQUEST))

    assert second.id == first.id
    assert len(db.list_transactions()) == 1
    assert publisher.last(CMD_PRINT_TICKET).get("ticket_no") == first.ticket_no


def test_repeat_press_does_not_hit_the_camera_again(service, camera, clock):
    service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    clock.advance(seconds=2)
    service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    assert len(camera.calls) == 1


def test_press_after_the_window_issues_a_new_ticket(service, clock, db):
    first = service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    clock.advance(seconds=30)
    second = service.on_ticket_request(evt(EVT_TICKET_REQUEST))

    assert second.id != first.id
    assert len(db.list_transactions()) == 2


def test_next_car_is_not_debounced_once_the_first_has_entered(service, clock, db):
    """Debounce keys on a PENDING ticket, so a completed entry never blocks."""
    first = service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    service.on_ticket_printed(evt(EVT_TICKET_PRINTED, barcode=first.barcode))
    service.on_vehicle_passed(evt(EVT_VEHICLE_PASSED, barcode=first.barcode))

    clock.advance(seconds=1)
    second = service.on_ticket_request(evt(EVT_TICKET_REQUEST))

    assert second.id != first.id


def test_debounce_can_be_disabled(db, camera, publisher, clock):
    service = make_service(
        make_config(button_debounce_seconds=0), db, camera, publisher, clock
    )
    first = service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    second = service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    assert first.id != second.id


# -- events referring to nothing -------------------------------------------


def test_printed_event_for_unknown_ticket_is_ignored(service, publisher):
    service.on_ticket_printed(evt(EVT_TICKET_PRINTED, barcode="000000000000"))
    assert publisher.sent == []


def test_vehicle_passed_for_unknown_ticket_is_ignored(service, publisher):
    service.on_vehicle_passed(evt(EVT_VEHICLE_PASSED, barcode="000000000000"))
    assert publisher.sent == []


def test_duplicate_vehicle_passed_is_harmless(service, db):
    transaction = service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    passed = evt(EVT_VEHICLE_PASSED, barcode=transaction.barcode)
    service.on_vehicle_passed(passed)
    service.on_vehicle_passed(passed)
    assert db.get(transaction.id).status == STATUS_ACTIVE


def test_events_are_recorded_for_the_audit_trail(service, db):
    transaction = service.on_ticket_request(evt(EVT_TICKET_REQUEST))
    types = [e["type"] for e in db.list_events(transaction_id=transaction.id)]
    assert "ticket_issued" in types
