"""Check-out: ticket scan -> plate capture -> match -> fee -> barrier.

Mirrors :mod:`trafix.server.checkin`: no transport of its own, everything is
injected, so the whole state machine is testable without a broker.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from trafix.config import Config
from trafix.db import (
    STATUS_ACTIVE,
    STATUS_AWAITING_PAYMENT,
    STATUS_CLOSED,
    Database,
    Transaction,
)
from trafix.envelope import CMD_CHECKOUT_RESULT, CMD_OPEN_GATE, Envelope
from trafix.server.checkin import Capturer, Publisher
from trafix.tariff import calculate_between
from trafix.tickets import PLATE_UNKNOWN, plates_match

log = logging.getLogger("checkout")

SOURCE = "checkout"

# Why a check-out was refused. Sent to the terminal so it can show something
# useful to the driver rather than just staying shut.
ERR_UNKNOWN_TICKET = "unknown_ticket"
ERR_ALREADY_CLOSED = "already_closed"
ERR_NOT_ACTIVE = "not_active"
ERR_PLATE_MISMATCH = "plate_mismatch"
ERR_LPR_FAILED = "lpr_read_failed"
ERR_NO_MATCHING_ENTRY = "no_matching_entry"

FLAG_PLATE_MISMATCH = "plate_mismatch"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CheckoutService:
    """Handles every event arriving on an exit lane."""

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

    def on_checkout_request(self, message: Envelope) -> Transaction | None:
        """A ticket was scanned at the exit (or a lost ticket declared)."""
        barcode = str(message.get("barcode") or "").strip()
        lost_ticket = bool(message.get("lost_ticket", False))

        self.db.log_event(
            source=SOURCE, type="checkout_request", lane=self.lane, detail=message.payload
        )

        read = self.lpr.capture(trigger="checkout", lane=self.lane)
        if not read.ok:
            log.warning("lane %s: exit plate unreadable (%s)", self.lane, read.reason)

        transaction = self._find_transaction(barcode, lost_ticket, read.plate)
        if transaction is None:
            reason = ERR_NO_MATCHING_ENTRY if lost_ticket else ERR_UNKNOWN_TICKET
            return self._refuse(message, reason, barcode=barcode, read=read)

        if transaction.status == STATUS_CLOSED:
            return self._refuse(
                message, ERR_ALREADY_CLOSED, transaction=transaction, read=read
            )

        if transaction.status not in (STATUS_ACTIVE, STATUS_AWAITING_PAYMENT):
            # PENDING means the car never actually completed its entry.
            return self._refuse(
                message, ERR_NOT_ACTIVE, transaction=transaction, read=read
            )

        match = plates_match(transaction.entry_plate, read.plate)
        policy = self.config.policies.plate_mismatch

        if match is False and policy == "deny":
            log.warning(
                "lane %s: %s entered as %s but exits as %s, refusing",
                self.lane,
                transaction.ticket_no,
                transaction.entry_plate,
                read.plate,
            )
            self.db.update(
                transaction.id, flagged=True, flag_reason=FLAG_PLATE_MISMATCH
            )
            return self._refuse(
                message, ERR_PLATE_MISMATCH, transaction=transaction, read=read
            )

        exit_moment = self.clock()
        fee = calculate_between(
            self.config.tariff,
            transaction.entry_datetime(),
            exit_moment,
            lost_ticket=lost_ticket,
        )

        flagged = transaction.flagged
        flag_reason = transaction.flag_reason
        if match is False and policy == "flag":
            flagged = True
            flag_reason = FLAG_PLATE_MISMATCH
            log.warning(
                "lane %s: %s plate mismatch (%s -> %s), flagged for review",
                self.lane,
                transaction.ticket_no,
                transaction.entry_plate,
                read.plate,
            )

        transaction = self.db.update(
            transaction.id,
            status=STATUS_AWAITING_PAYMENT,
            exit_lane=self.lane,
            exit_time=exit_moment.isoformat(timespec="milliseconds"),
            exit_plate=read.plate or PLATE_UNKNOWN,
            exit_confidence=read.confidence,
            exit_image_url=read.image_url,
            plate_match=match,
            duration_minutes=fee.duration_minutes,
            fee=fee.amount,
            flagged=flagged,
            flag_reason=flag_reason,
        )

        log.info(
            "lane %s: %s parked %s min, fee %s (%s)",
            self.lane,
            transaction.ticket_no,
            fee.duration_minutes,
            fee.formatted(),
            fee.breakdown,
        )
        self.db.log_event(
            source=SOURCE,
            type="fee_quoted",
            lane=self.lane,
            transaction_id=transaction.id,
            detail={
                "fee": fee.amount,
                "duration_minutes": fee.duration_minutes,
                "breakdown": fee.breakdown,
                "plate_match": match,
                "lost_ticket": lost_ticket,
            },
        )

        self.publish(
            self.lane,
            message.reply(
                CMD_CHECKOUT_RESULT,
                device_id="server",
                payload={
                    "ticket_no": transaction.ticket_no,
                    "barcode": transaction.barcode,
                    "plate_in": transaction.entry_plate,
                    "plate_out": transaction.exit_plate,
                    "plate_match": match,
                    "entry_time": transaction.entry_time,
                    "exit_time": transaction.exit_time,
                    "duration_minutes": fee.duration_minutes,
                    "fee": fee.amount,
                    "currency": fee.currency,
                    "breakdown": fee.breakdown,
                    "lost_ticket": lost_ticket,
                    "flagged": flagged,
                    "image_url": transaction.exit_image_url,
                },
            ),
        )
        return transaction

    def on_payment_settled(self, message: Envelope) -> None:
        """Payment taken (or the fee was zero). Let the car out."""
        transaction = self._lookup(message)
        if transaction is None:
            return

        if transaction.status != STATUS_AWAITING_PAYMENT:
            log.warning(
                "lane %s: payment for %s which is %s, ignoring",
                self.lane,
                transaction.ticket_no,
                transaction.status,
            )
            return

        amount = message.get("amount", transaction.fee)
        self.db.update(transaction.id, paid=True)
        self.db.log_event(
            source=SOURCE,
            type="payment_settled",
            lane=self.lane,
            transaction_id=transaction.id,
            detail={"amount": amount, "method": message.get("method")},
        )
        log.info(
            "lane %s: %s paid %s, opening barrier",
            self.lane,
            transaction.ticket_no,
            amount,
        )

        self.publish(
            self.lane,
            message.reply(
                CMD_OPEN_GATE,
                device_id="server",
                payload={
                    "ticket_no": transaction.ticket_no,
                    "barcode": transaction.barcode,
                    "direction": "exit",
                },
            ),
        )

    def on_vehicle_passed(self, message: Envelope) -> None:
        """The car has left. Close the transaction."""
        transaction = self._lookup(message)
        if transaction is None:
            return

        if transaction.status == STATUS_CLOSED:
            return

        self.db.update(transaction.id, status=STATUS_CLOSED)
        self.db.log_event(
            source=SOURCE,
            type="exit_completed",
            lane=self.lane,
            transaction_id=transaction.id,
        )
        log.info("lane %s: %s CLOSED", self.lane, transaction.ticket_no)

    # -- helpers -----------------------------------------------------------

    def _find_transaction(
        self, barcode: str, lost_ticket: bool, plate: str | None
    ) -> Transaction | None:
        """Locate the entry record: by barcode normally, by plate if lost."""
        if barcode:
            found = self.db.get_by_barcode(barcode)
            if found is not None:
                return found
            log.warning("lane %s: barcode %s is not in the database", self.lane, barcode)

        if lost_ticket and plate:
            found = self.db.find_active_by_plate(plate)
            if found is not None:
                log.info(
                    "lane %s: lost ticket resolved to %s by plate %s",
                    self.lane,
                    found.ticket_no,
                    plate,
                )
                return found
            log.warning("lane %s: no active entry for plate %s", self.lane, plate)

        return None

    def _refuse(
        self,
        message: Envelope,
        error: str,
        *,
        transaction: Transaction | None = None,
        barcode: str | None = None,
        read=None,
    ) -> None:
        log.warning("lane %s: check-out refused (%s)", self.lane, error)
        self.db.log_event(
            source=SOURCE,
            type="checkout_refused",
            lane=self.lane,
            transaction_id=transaction.id if transaction else None,
            detail={
                "error": error,
                "barcode": barcode or (transaction.barcode if transaction else None),
                "exit_plate": read.plate if read else None,
            },
        )
        self.publish(
            self.lane,
            message.reply(
                CMD_CHECKOUT_RESULT,
                device_id="server",
                payload={
                    "error": error,
                    "text": _MESSAGES.get(error, "Please call the operator."),
                    "barcode": barcode or (transaction.barcode if transaction else None),
                    "ticket_no": transaction.ticket_no if transaction else None,
                    "plate_out": read.plate if read else None,
                },
            ),
        )
        return None

    def _lookup(self, message: Envelope) -> Transaction | None:
        barcode = message.get("barcode")
        if barcode:
            found = self.db.get_by_barcode(str(barcode))
            if found is not None:
                return found

        ticket_no = message.get("ticket_no")
        if ticket_no:
            found = self.db.get_by_ticket(str(ticket_no))
            if found is not None:
                return found

        log.warning(
            "lane %s: %s refers to an unknown ticket (barcode=%r ticket_no=%r)",
            self.lane,
            message.type,
            barcode,
            ticket_no,
        )
        return None


_MESSAGES = {
    ERR_UNKNOWN_TICKET: "Ticket not recognised. Please call the operator.",
    ERR_ALREADY_CLOSED: "This ticket has already been used.",
    ERR_NOT_ACTIVE: "No completed entry for this ticket.",
    ERR_PLATE_MISMATCH: "Plate does not match the entry record.",
    ERR_NO_MATCHING_ENTRY: "No parked vehicle found for this plate.",
}
