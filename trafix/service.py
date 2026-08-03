"""The business logic, independent of HTTP.

Ports ``GateController::gatein`` and the gate-out family from the Laravel app,
including ``GateOutLpr`` — the method the routes file points at but which was
never written (flow.md §7.1).

Keeping this out of the FastAPI layer means the whole check-in/check-out cycle
can be tested with a database and nothing else: no HTTP, no broker.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from trafix import escpos, rates
from trafix.models import (
    GATE_STATUS_IN,
    GATE_STATUS_OUT,
    PAYMENT_PAID,
    STATUS_GATEIN,
    TYPE_QR_CASH,
    TYPE_QR_PAYMENT,
    GateEvents,
    Locations,
    Members,
    ParkingFees,
    Transactions,
    Vehicles,
    XenditQrPool,
)

log = logging.getLogger("service")

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Result statuses, matching the strings the Laravel API returns so an existing
# frontend keeps working.
STATUS_SUCCESS = "success"
STATUS_SUCCESS_MEMBER = "success_member"
STATUS_SUCCESS_TICKET = "success_ticket"
STATUS_TICKET_USED = "ticket_used"
STATUS_NOT_FOUND = "notfound"
STATUS_ALREADY_PAID = "already_paid"
STATUS_PLATE_MISMATCH = "plate_mismatch"
STATUS_MEMBER_EXPIRED = "member_expired"


class Publisher(Protocol):
    """How the service reaches the gate hardware."""

    def print_ticket(self, gate: str, blocks: list[dict], message_id: str) -> None: ...

    def open_barrier(self, gate: str, *, exit_lane: bool = False) -> None: ...


class NullPublisher:
    """Used in tests and when the service runs without a broker."""

    def __init__(self) -> None:
        self.printed: list[tuple[str, list[dict]]] = []
        self.barriers: list[tuple[str, bool]] = []

    def print_ticket(self, gate: str, blocks: list[dict], message_id: str) -> None:
        self.printed.append((gate, blocks))

    def open_barrier(self, gate: str, *, exit_lane: bool = False) -> None:
        self.barriers.append((gate, exit_lane))


@dataclass
class GateInResult:
    status: str
    transaction_code: str
    transaction_id: int
    plate: str | None
    image_path: str
    type_qr: str


@dataclass
class MemberGateInResult:
    """The outcome of an RFID member auto-entry."""

    status: str
    transaction_code: str | None = None
    member_name: str | None = None
    member_code: str | None = None
    plate: str | None = None
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_SUCCESS


@dataclass
class GateOutResult:
    status: str
    transaction_code: str | None = None
    total: float = 0.0
    duration: str = ""
    plate_in: str | None = None
    plate_out: str | None = None
    plate_match: bool | None = None
    is_member: bool = False
    member_name: str | None = None
    time_checkin: str | None = None
    time_checkout: str | None = None
    cam_in: str | None = None
    cam_out: str | None = None
    breakdown: str = ""
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in (
            STATUS_SUCCESS,
            STATUS_SUCCESS_MEMBER,
            STATUS_SUCCESS_TICKET,
        )


def now_string() -> str:
    return datetime.now().strftime(DATETIME_FORMAT)


def normalize_plate(plate: str | None) -> str | None:
    """Strip spacing and case so two reads can be compared.

    flow.md §7.7: the entry and exit cameras produce different strings for what
    may be the same vehicle. Normalising is necessary but nowhere near
    sufficient — which is why the plate is advisory, not a lookup key.
    """
    if not plate:
        return None
    cleaned = "".join(ch for ch in plate.upper() if ch.isalnum())
    return cleaned or None


class ParkingService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        publisher: Publisher | None = None,
        storage=None,
        config=None,
        clock: Callable[[], datetime] = datetime.now,
        print_gap_seconds: float = 0.2,
    ) -> None:
        self.session_factory = session_factory
        self.publisher = publisher or NullPublisher()
        self.storage = storage
        self.config = config
        self.clock = clock
        # The gap between the two halves of the ticket. The real controller
        # drops the second half if they arrive back to back, which is why the
        # PHP has usleep(200000). Tests set this to zero.
        self.print_gap_seconds = print_gap_seconds

    # -- helpers -----------------------------------------------------------

    def generate_transaction_code(self, session: Session) -> str:
        """Port of ``generateTrxCode()``: 7 digits of epoch ms + 3 random.

        Collision-checked against the table, as the original is.
        """
        milliseconds = int(time.time() * 1000)
        time_part = str(milliseconds)[-7:]
        for _ in range(1000):
            candidate = f"{time_part}{random.randint(0, 999):03d}"
            exists = session.scalar(
                select(Transactions.transaction_id).where(
                    Transactions.transaction_code == candidate
                )
            )
            if not exists:
                return candidate
        raise RuntimeError("could not allocate a unique transaction code")

    def _log_event(
        self,
        session: Session,
        *,
        source: str,
        method: str,
        gate: str | None = None,
        transaction_code: str | None = None,
        detail: str | None = None,
    ) -> None:
        session.add(
            GateEvents(
                source=source,
                method=method,
                gate=gate,
                transaction_code=transaction_code,
                detail=detail,
            )
        )

    # -- check-in ----------------------------------------------------------

    def gate_in(
        self,
        *,
        gate: str,
        vehicle_id: int | None,
        plate_num: str | None,
        url_gambar: str | None,
        serial_no: str,
        ipcam: str | None = None,
    ) -> GateInResult:
        """A driver pressed the ticket button. Port of ``gatein()``."""
        with self.session_factory() as session:
            location = session.scalar(select(Locations).limit(1))
            if location is None:
                raise RuntimeError("no row in 'locations' — the site is not configured")

            transaction_code = self.generate_transaction_code(session)

            # Fetch the snapshot the LPR advertised. Off the request path: a
            # slow camera must never hold the barrier shut.
            image_path = "-"
            if url_gambar and self.storage is not None:
                image_path = self.storage.download_async(
                    url_gambar,
                    "lpr/gatein",
                    self.storage.lpr_filename(url_gambar),
                )
            elif url_gambar:
                image_path = url_gambar

            type_qr, qr_string, pr_id = self._allocate_qr(
                session, location, vehicle_id, transaction_code
            )

            plate = normalize_plate(plate_num)
            checkin_at = self.clock().strftime(DATETIME_FORMAT)

            transaction = Transactions(
                transaction_code=transaction_code,
                time_checkin=checkin_at,
                status=STATUS_GATEIN,
                gate_in=gate,
                gate_status=GATE_STATUS_IN,
                vehicle_id=vehicle_id,
                qrcode=qr_string,
                pr_id_xendit=pr_id,
                cam_in=image_path,
                camin_lpr=image_path,
                id_userlocations=location.id_userlocations,
                police_number=plate,
                payment_type="cash" if type_qr == TYPE_QR_CASH else "qris",
            )
            session.add(transaction)
            session.flush()

            self._log_event(
                session,
                source="api",
                method="gatein",
                gate=gate,
                transaction_code=transaction_code,
                detail=f"plate={plate or '(none)'} typeqr={type_qr}",
            )

            blocks_1, blocks_2 = self._build_ticket(
                session,
                location=location,
                gate=gate,
                transaction_code=transaction_code,
                qr_string=qr_string,
                type_qr=type_qr,
                vehicle_id=vehicle_id,
                plate=plate,
                checkin_at=checkin_at,
            )
            transaction_id = transaction.transaction_id
            session.commit()

        # Two publishes, separated exactly as the PHP's usleep(200000) does.
        from trafix.protocol import message_id

        self.publisher.print_ticket(gate, blocks_1, message_id(transaction_code, 1))
        if self.print_gap_seconds:
            time.sleep(self.print_gap_seconds)
        self.publisher.print_ticket(gate, blocks_2, message_id(transaction_code, 2))

        log.info(
            "gate %s: issued ticket %s for plate %s",
            gate,
            transaction_code,
            plate or "(none)",
        )
        return GateInResult(
            status=STATUS_SUCCESS,
            transaction_code=transaction_code,
            transaction_id=transaction_id,
            plate=plate,
            image_path=image_path,
            type_qr=type_qr,
        )

    def member_gate_in(
        self,
        *,
        gate: str,
        card_no: str,
        serial_no: str,
        vehicle_id: int | None = None,
    ) -> MemberGateInResult:
        """A member tapped an RFID card at the entry. No ticket is printed.

        Mirrors the on-site ``readCard`` event (flow.md §5): the tag is resolved
        against ``Members.card_number``; an active member for this vehicle class
        gets a transaction and the barrier opens. Unknown cards and expired
        subscriptions are rejected — the barrier stays shut.
        """
        card_no = str(card_no or "").strip()
        if not card_no:
            return MemberGateInResult(
                status=STATUS_NOT_FOUND, message="No card number supplied"
            )

        with self.session_factory() as session:
            member = session.scalar(
                select(Members).where(Members.card_number == card_no)
            )
            if member is None:
                log.info("gate %s: card %s is not a member", gate, card_no)
                return MemberGateInResult(
                    status=STATUS_NOT_FOUND,
                    message=f"No member found for card {card_no}",
                )

            if not rates.is_active_member(member, vehicle_id):
                log.warning(
                    "gate %s: card %s belongs to %s but the subscription is "
                    "expired or the vehicle class mismatches",
                    gate,
                    card_no,
                    member.name,
                )
                return MemberGateInResult(
                    status=STATUS_MEMBER_EXPIRED,
                    member_name=member.name,
                    member_code=member.member_code,
                    plate=member.police_number,
                    message="Member subscription expired or vehicle class mismatch",
                )

            location = session.scalar(select(Locations).limit(1))
            if location is None:
                raise RuntimeError("no row in 'locations' — the site is not configured")

            transaction_code = self.generate_transaction_code(session)
            checkin_at = self.clock().strftime(DATETIME_FORMAT)

            transaction = Transactions(
                transaction_code=transaction_code,
                time_checkin=checkin_at,
                status=STATUS_GATEIN,
                gate_in=gate,
                gate_status=GATE_STATUS_IN,
                vehicle_id=member.vehicle_id or vehicle_id,
                police_number=member.police_number,
                card_number=card_no,
                type="member",
                payment_status=PAYMENT_PAID,
                total=0,
                id_userlocations=location.id_userlocations,
                cam_in="-",
                camin_lpr="-",
                payment_type="cash",
            )
            session.add(transaction)
            session.flush()

            self._log_event(
                session,
                source="api",
                method="gatein-card",
                gate=gate,
                transaction_code=transaction_code,
                detail=f"card={card_no} member={member.name}",
            )
            session.commit()

        log.info(
            "gate %s: member %s entered on card %s (ticket %s)",
            gate,
            member.name,
            card_no,
            transaction_code,
        )
        return MemberGateInResult(
            status=STATUS_SUCCESS,
            transaction_code=transaction_code,
            member_name=member.name,
            member_code=member.member_code,
            plate=member.police_number,
        )

    def _allocate_qr(
        self,
        session: Session,
        location: Locations,
        vehicle_id: int | None,
        transaction_code: str,
    ) -> tuple[str, str, str | None]:
        """Draw a QRIS code from the pool, or fall back to a cash ticket.

        The pool only applies when the location is activated for online
        payment. When it is empty the ticket silently becomes a cash one — on
        site that happens whenever the queue worker is not running (§2).
        """
        if not (
            location.status == "active"
            and location.id_userlocations is not None
            and vehicle_id is not None
        ):
            return TYPE_QR_CASH, transaction_code, None

        qr = session.scalar(
            select(XenditQrPool)
            .where(
                XenditQrPool.vehicle_id == vehicle_id,
                XenditQrPool.status == "available",
                XenditQrPool.expired_at > datetime.now(),
            )
            .order_by(XenditQrPool.id)
            .limit(1)
        )
        if qr is None:
            log.warning(
                "QRIS pool empty for vehicle %s — falling back to a cash ticket",
                vehicle_id,
            )
            return TYPE_QR_CASH, transaction_code, None

        qr.status = "used"
        return TYPE_QR_PAYMENT, qr.qr_string, qr.pr_id

    def _build_ticket(
        self,
        session: Session,
        *,
        location: Locations,
        gate: str,
        transaction_code: str,
        qr_string: str,
        type_qr: str,
        vehicle_id: int | None,
        plate: str | None,
        checkin_at: str,
    ) -> tuple[list[dict], list[dict]]:
        motor = session.scalar(select(ParkingFees).where(ParkingFees.vehicle_id == 1))
        mobil = session.scalar(select(ParkingFees).where(ParkingFees.vehicle_id == 2))
        vehicle = (
            session.scalar(select(Vehicles).where(Vehicles.vehicle_id == vehicle_id))
            if vehicle_id
            else None
        )

        header = escpos.TicketHeader(
            store_name=location.name,
            store_address=location.address,
            qris=qr_string,
            type_qr=type_qr,
        )
        body = escpos.TicketBody(
            gate=gate,
            datetime=checkin_at,
            trx=transaction_code,
            vehicle=vehicle.name if vehicle else None,
            lost_motor=motor.ticket_charge if motor else 0,
            lost_car=mobil.ticket_charge if mobil else 0,
            stay_motor=motor.stay_charge if motor else 0,
            stay_car=mobil.stay_charge if mobil else 0,
            police_number=plate,
            type_qr=type_qr,
        )
        return escpos.build_gate_in_1(header), escpos.build_gate_in_2(body)

    # -- lookup ------------------------------------------------------------

    def find_open_transaction(
        self, session: Session, *, code: str | None = None, plate: str | None = None
    ) -> Transactions | None:
        """Locate the parking session a vehicle at the exit belongs to.

        **The ticket code is authoritative and the plate is advisory.** flow.md
        §7.7 shows why: the two cameras disagree, and 4 of 6 entry tickets
        recorded no plate at all, so a plate can never be the primary key.

        ``time_checkout IS NULL`` decides whether a car is still inside, not
        ``gate_status`` — see :meth:`Transactions.is_inside`.
        """
        if code:
            # qrcode first, matching CalculateRate's own lookup: for a QRIS
            # ticket the printed code is the QR string, not the trx code.
            transaction = session.scalar(
                select(Transactions)
                .where(
                    (Transactions.qrcode == code)
                    | (Transactions.transaction_code == code)
                )
                .order_by(Transactions.transaction_id.desc())
                .limit(1)
            )
            if transaction is not None:
                return transaction

        normalized = normalize_plate(plate)
        if normalized:
            return session.scalar(
                select(Transactions)
                .where(
                    Transactions.police_number == normalized,
                    Transactions.time_checkout.is_(None),
                )
                .order_by(Transactions.time_checkin.desc())
                .limit(1)
            )
        return None

    def quote(
        self, *, code: str | None = None, plate: str | None = None, lost: bool = False
    ) -> GateOutResult:
        """What would this vehicle pay? Read-only — nothing is written.

        Backs the cashier's ``detailtransaction`` screen.
        """
        with self.session_factory() as session:
            transaction = self.find_open_transaction(session, code=code, plate=plate)
            if transaction is None:
                return GateOutResult(
                    status=STATUS_NOT_FOUND,
                    message="Active transaction not found",
                )
            return self._price(session, transaction, plate_out=plate, lost=lost)

    def _price(
        self,
        session: Session,
        transaction: Transactions,
        *,
        plate_out: str | None,
        lost: bool = False,
    ) -> GateOutResult:
        tariff_row = session.scalar(
            select(ParkingFees).where(ParkingFees.vehicle_id == transaction.vehicle_id)
        )
        member = session.scalar(
            select(Members).where(
                Members.police_number == (transaction.police_number or "\x00")
            )
        )

        check_in = datetime.strptime(transaction.time_checkin, DATETIME_FORMAT)
        check_out = self.clock()

        if tariff_row is None:
            log.error("no parking_fees row for vehicle %s", transaction.vehicle_id)
            fee = rates.Fee(0, "0", "no tariff configured")
        else:
            fee = rates.calculate(
                rates.Tariff.from_row(tariff_row),
                check_in,
                check_out,
                vehicle_id=transaction.vehicle_id,
                member=member,
                type=rates.TYPE_LOST if lost else rates.TYPE_NORMAL,
            )

        plate_in = normalize_plate(transaction.police_number)
        plate_seen = normalize_plate(plate_out)
        match = None if (plate_in is None or plate_seen is None) else plate_in == plate_seen

        is_member = rates.is_active_member(member, transaction.vehicle_id)

        return GateOutResult(
            status=STATUS_SUCCESS_MEMBER if is_member else STATUS_SUCCESS,
            transaction_code=transaction.transaction_code,
            total=fee.total,
            duration=fee.duration,
            plate_in=plate_in,
            plate_out=plate_seen,
            plate_match=match,
            is_member=is_member,
            member_name=member.name if member else None,
            time_checkin=transaction.time_checkin,
            time_checkout=check_out.strftime(DATETIME_FORMAT),
            cam_in=transaction.cam_in,
            cam_out=transaction.cam_out,
            breakdown=fee.breakdown,
        )

    # -- check-out ---------------------------------------------------------

    def gate_out(
        self,
        *,
        gate: str,
        code: str | None = None,
        plate_num: str | None = None,
        url_gambar: str | None = None,
        admin_id: int | None = None,
        shift_id: int | None = None,
        lost: bool = False,
        open_barrier: bool = True,
    ) -> GateOutResult:
        """Settle a parking session and release the vehicle.

        **This is the method missing from production** (flow.md §7.1): the
        route ``POST /api/lpr/gateout`` points at ``GateoutController::GateOutLpr``,
        which does not exist, so every automated exit 500s. Modelled on
        ``GateOutRfidLpr``.
        """
        with self.session_factory() as session:
            transaction = self.find_open_transaction(
                session, code=code, plate=plate_num
            )

            if transaction is None:
                self._log_event(
                    session,
                    source="api",
                    method="gateout_notfound",
                    gate=gate,
                    detail=f"code={code!r} plate={plate_num!r}",
                )
                session.commit()
                return GateOutResult(
                    status=STATUS_NOT_FOUND,
                    message="Active transaction not found for this ticket or plate",
                )

            if transaction.is_paid() and transaction.time_checkout is not None:
                return GateOutResult(
                    status=STATUS_TICKET_USED,
                    transaction_code=transaction.transaction_code,
                    message="This ticket has already been used",
                )

            quote = self._price(session, transaction, plate_out=plate_num, lost=lost)

            # Optional strictness. Off by default, because on site the plates
            # genuinely disagree and refusing would strand real drivers.
            if (
                self.config is not None
                and self.config.policies.require_plate_match
                and quote.plate_match is False
            ):
                self._log_event(
                    session,
                    source="api",
                    method="gateout_plate_mismatch",
                    gate=gate,
                    transaction_code=transaction.transaction_code,
                    detail=f"in={quote.plate_in} out={quote.plate_out}",
                )
                session.commit()
                return GateOutResult(
                    status=STATUS_PLATE_MISMATCH,
                    transaction_code=transaction.transaction_code,
                    plate_in=quote.plate_in,
                    plate_out=quote.plate_out,
                    plate_match=False,
                    message="Plate does not match the entry record",
                )

            image_path = transaction.cam_out
            if url_gambar and self.storage is not None:
                image_path = self.storage.download_async(
                    url_gambar,
                    "lpr/gateout",
                    self.storage.lpr_filename(url_gambar, prefix="CAMOUT_LPR"),
                )
            elif url_gambar:
                image_path = url_gambar

            transaction.time_checkout = quote.time_checkout
            transaction.total = quote.total
            transaction.duration = quote.duration
            transaction.payment_status = PAYMENT_PAID
            transaction.paid_at = self.clock()
            transaction.gate_out = gate
            transaction.gate_status = GATE_STATUS_OUT
            transaction.admin_id = admin_id
            transaction.shift_id = shift_id
            if image_path:
                transaction.cam_out = image_path
                transaction.camout_lpr = image_path
            # The exit read is recorded but never overwrites a known entry
            # plate: the entry read is the one the ticket was issued against.
            if quote.plate_out and not transaction.police_number:
                transaction.police_number = quote.plate_out
            if quote.plate_match is False:
                transaction.keterangan = (
                    f"plate mismatch: entered {quote.plate_in}, "
                    f"exited {quote.plate_out}"
                )

            self._log_event(
                session,
                source="api",
                method="gateout",
                gate=gate,
                transaction_code=transaction.transaction_code,
                detail=(
                    f"total={quote.total} duration={quote.duration} "
                    f"match={quote.plate_match}"
                ),
            )
            session.commit()

        # FIX for flow.md §7.6: production never commands the exit barrier.
        if open_barrier and (
            self.config is None or self.config.policies.command_exit_barrier
        ):
            self.publisher.open_barrier(gate, exit_lane=True)

        log.info(
            "gate %s: %s settled, total %s, duration %s",
            gate,
            quote.transaction_code,
            quote.total,
            quote.duration,
        )
        result = GateOutResult(
            **{
                **quote.__dict__,
                "status": (
                    STATUS_SUCCESS_MEMBER if quote.is_member else STATUS_SUCCESS_TICKET
                ),
                "cam_out": image_path,
            }
        )
        return result
