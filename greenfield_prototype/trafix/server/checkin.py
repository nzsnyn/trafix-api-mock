"""Check-in: button press -> plate capture -> ticket -> barrier.

The service owns no transport of its own. It is handed a way to publish and a
way to capture, which is what lets the tests drive the whole state machine
without a broker or a camera.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable, Protocol

from trafix.config import Config
from trafix.db import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    Database,
    Transaction,
)
from trafix.envelope import (
    CMD_OPEN_GATE,
    CMD_PRINT_TICKET,
    CMD_REJECT,
    Envelope,
)
from trafix.lpr_client import PlateRead
from trafix.tickets import PLATE_UNKNOWN, issue

log = logging.getLogger("checkin")

SOURCE = "checkin"

# Reasons a transaction gets flagged for an operator to look at later.
FLAG_LPR_FAILED = "lpr_read_failed"


class Capturer(Protocol):
    def capture(self, *, trigger: str = ..., lane: str | None = ...) -> PlateRead: ...


Publisher = Callable[[str, Envelope], None]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CheckinService:
    """Handles every event arriving on an entry lane."""

    def __init__(
        self,
        *,
        lane: str,
        config: Config,
        db: Database,
        lpr: Capturer,
        publish: Publisher,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.lane = lane
        self.config = config
        self.db = db
        self.lpr = lpr
        self.publish = publish
        self.clock = clock

    # -- events ------------------------------------------------------------

    def on_ticket_request(self, message: Envelope) -> Transaction | None:
        """A driver pressed the button."""
        self.db.log_event(
            source=SOURCE, type="ticket_request", lane=self.lane, detail=message.payload
        )

        repeat = self._debounced_ticket()
        if repeat is not None:
            log.info(
                "lane %s: repeat press within %.0fs, reissuing %s",
                self.lane,
                self.config.policies.button_debounce_seconds,
                repeat.ticket_no,
            )
            self._send_print_ticket(repeat, message)
            return repeat

        read = self.lpr.capture(trigger="button", lane=self.lane)

        if not read.ok and self.config.policies.lpr_failure == "deny":
            log.warning("lane %s: plate unreadable (%s), refusing entry", self.lane, read.reason)
            self.db.log_event(
                source=SOURCE,
                type="entry_refused",
                lane=self.lane,
                detail=read.as_detail(),
            )
            self.publish(
                self.lane,
                message.reply(
                    CMD_REJECT,
                    device_id="server",
                    payload={
                        "reason": "plate_unreadable",
                        "text": "Plate could not be read. Please call the operator.",
                        "detail": read.reason,
                    },
                ),
            )
            return None

        # Policy is "allow": issue a ticket anyway rather than trap the car,
        # but record that the plate is not trustworthy.
        #
        # A failed read must never contribute a plate, even when the camera did
        # return one. A low-confidence guess stored here would be compared
        # against the exit read later and produce a mismatch nobody can explain.
        # The rejected value is still in the event log if an operator needs it.
        flagged = not read.ok
        plate = read.plate if read.ok else PLATE_UNKNOWN
        if flagged:
            log.warning(
                "lane %s: plate unreadable (%s), issuing ticket with plate %s",
                self.lane,
                read.reason,
                PLATE_UNKNOWN,
            )

        moment = self.clock()
        sequence = self.db.next_sequence(f"ticket_{self.lane}")
        ticket_no, barcode = issue(self.lane, sequence, moment)

        transaction = self.db.create_entry(
            ticket_no=ticket_no,
            barcode=barcode,
            lane=self.lane,
            entry_time=moment.isoformat(timespec="milliseconds"),
            plate=plate,
            confidence=read.confidence,
            image_url=read.image_url,
            flagged=flagged,
            flag_reason=f"{FLAG_LPR_FAILED}:{read.reason}" if flagged else None,
        )

        log.info(
            "lane %s: issued %s for plate %s (conf %s)",
            self.lane,
            ticket_no,
            plate,
            f"{read.confidence:.2f}" if read.confidence is not None else "n/a",
        )
        self.db.log_event(
            source=SOURCE,
            type="ticket_issued",
            lane=self.lane,
            transaction_id=transaction.id,
            detail={"ticket_no": ticket_no, **read.as_detail()},
        )

        self._send_print_ticket(transaction, message)
        return transaction

    def on_ticket_printed(self, message: Envelope) -> None:
        """The terminal confirmed the ticket is in the driver's hand."""
        transaction = self._lookup(message)
        if transaction is None:
            return

        self.db.log_event(
            source=SOURCE,
            type="ticket_printed",
            lane=self.lane,
            transaction_id=transaction.id,
        )
        log.info("lane %s: %s printed, opening barrier", self.lane, transaction.ticket_no)

        self.publish(
            self.lane,
            message.reply(
                CMD_OPEN_GATE,
                device_id="server",
                payload={
                    "ticket_no": transaction.ticket_no,
                    "barcode": transaction.barcode,
                    "direction": "entry",
                },
            ),
        )

    def on_vehicle_passed(self, message: Envelope) -> None:
        """The loop detector cleared: the car is inside."""
        transaction = self._lookup(message)
        if transaction is None:
            return

        if transaction.status != STATUS_PENDING:
            log.debug(
                "lane %s: %s already %s, ignoring pass",
                self.lane,
                transaction.ticket_no,
                transaction.status,
            )
            return

        self.db.update(transaction.id, status=STATUS_ACTIVE)
        self.db.log_event(
            source=SOURCE,
            type="entry_completed",
            lane=self.lane,
            transaction_id=transaction.id,
        )
        log.info("lane %s: %s is now ACTIVE", self.lane, transaction.ticket_no)

    # -- helpers -----------------------------------------------------------

    def _send_print_ticket(self, transaction: Transaction, cause: Envelope) -> None:
        self.publish(
            self.lane,
            cause.reply(
                CMD_PRINT_TICKET,
                device_id="server",
                payload={
                    "ticket_no": transaction.ticket_no,
                    "barcode": transaction.barcode,
                    "plate": transaction.entry_plate,
                    "image_url": transaction.entry_image_url,
                    "entry_time": transaction.entry_time,
                    "flagged": transaction.flagged,
                },
            ),
        )

    def _debounced_ticket(self) -> Transaction | None:
        """Return the last ticket if this press is a bounce of the same one.

        Drivers press twice when the printer is slow. Issuing a second ticket
        for one car leaves an orphan record that never checks out.
        """
        window = self.config.policies.button_debounce_seconds
        if window <= 0:
            return None

        latest = self.db.latest_entry_for_lane(self.lane)
        if latest is None or latest.status != STATUS_PENDING:
            return None

        age = (self.clock() - latest.entry_datetime()).total_seconds()
        return latest if 0 <= age <= window else None

    def _lookup(self, message: Envelope) -> Transaction | None:
        """Find the transaction a terminal event refers to."""
        barcode = message.get("barcode")
        if barcode:
            transaction = self.db.get_by_barcode(str(barcode))
            if transaction is not None:
                return transaction

        ticket_no = message.get("ticket_no")
        if ticket_no:
            transaction = self.db.get_by_ticket(str(ticket_no))
            if transaction is not None:
                return transaction

        log.warning(
            "lane %s: %s refers to an unknown ticket (barcode=%r ticket_no=%r)",
            self.lane,
            message.type,
            barcode,
            ticket_no,
        )
        return None
