"""SQLite persistence. The only module in the project that touches sqlite3.

One row per parked vehicle in ``transactions``; an append-only ``events`` table
records everything that happened to it, which is what you read when a driver
complains at the exit.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Transaction lifecycle.
STATUS_PENDING = "PENDING"  # ticket requested, not yet handed to the driver
STATUS_ACTIVE = "ACTIVE"  # vehicle is inside the car park
STATUS_AWAITING_PAYMENT = "AWAITING_PAYMENT"  # fee quoted at the exit
STATUS_CLOSED = "CLOSED"  # vehicle has left
STATUS_ABORTED = "ABORTED"  # entry never completed

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_no       TEXT    NOT NULL UNIQUE,
    barcode         TEXT    NOT NULL UNIQUE,
    status          TEXT    NOT NULL,

    entry_lane      TEXT    NOT NULL,
    entry_time      TEXT    NOT NULL,
    entry_plate     TEXT,
    entry_confidence REAL,
    entry_image_url TEXT,

    exit_lane       TEXT,
    exit_time       TEXT,
    exit_plate      TEXT,
    exit_confidence REAL,
    exit_image_url  TEXT,

    plate_match     INTEGER,
    duration_minutes INTEGER,
    fee             INTEGER,
    paid            INTEGER NOT NULL DEFAULT 0,

    flagged         INTEGER NOT NULL DEFAULT 0,
    flag_reason     TEXT,

    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tx_barcode ON transactions(barcode);
CREATE INDEX IF NOT EXISTS idx_tx_status ON transactions(status);
CREATE INDEX IF NOT EXISTS idx_tx_entry_plate ON transactions(entry_plate);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id  INTEGER,
    ts              TEXT    NOT NULL,
    lane            TEXT,
    source          TEXT    NOT NULL,
    type            TEXT    NOT NULL,
    detail          TEXT,
    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
);

CREATE INDEX IF NOT EXISTS idx_events_tx ON events(transaction_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS counters (
    name    TEXT PRIMARY KEY,
    value   INTEGER NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Transaction:
    id: int
    ticket_no: str
    barcode: str
    status: str
    entry_lane: str
    entry_time: str
    entry_plate: str | None
    entry_confidence: float | None
    entry_image_url: str | None
    exit_lane: str | None
    exit_time: str | None
    exit_plate: str | None
    exit_confidence: float | None
    exit_image_url: str | None
    plate_match: bool | None
    duration_minutes: int | None
    fee: int | None
    paid: bool
    flagged: bool
    flag_reason: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Transaction":
        return cls(
            id=row["id"],
            ticket_no=row["ticket_no"],
            barcode=row["barcode"],
            status=row["status"],
            entry_lane=row["entry_lane"],
            entry_time=row["entry_time"],
            entry_plate=row["entry_plate"],
            entry_confidence=row["entry_confidence"],
            entry_image_url=row["entry_image_url"],
            exit_lane=row["exit_lane"],
            exit_time=row["exit_time"],
            exit_plate=row["exit_plate"],
            exit_confidence=row["exit_confidence"],
            exit_image_url=row["exit_image_url"],
            plate_match=None if row["plate_match"] is None else bool(row["plate_match"]),
            duration_minutes=row["duration_minutes"],
            fee=row["fee"],
            paid=bool(row["paid"]),
            flagged=bool(row["flagged"]),
            flag_reason=row["flag_reason"],
        )

    def entry_datetime(self) -> datetime:
        return datetime.fromisoformat(self.entry_time)


class Database:
    """Thin repository over SQLite.

    A single connection guarded by a lock: the server is one process handling
    two lanes, so contention is negligible and this keeps transactions simple.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- counters ----------------------------------------------------------

    def next_sequence(self, name: str) -> int:
        """Atomically increment and return a named counter."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO counters(name, value) VALUES(?, 1) "
                "ON CONFLICT(name) DO UPDATE SET value = value + 1 "
                "RETURNING value",
                (name,),
            )
            value = int(cur.fetchone()[0])
            self._conn.commit()
            return value

    # -- transactions ------------------------------------------------------

    def create_entry(
        self,
        *,
        ticket_no: str,
        barcode: str,
        lane: str,
        entry_time: str,
        plate: str | None,
        confidence: float | None,
        image_url: str | None,
        flagged: bool = False,
        flag_reason: str | None = None,
    ) -> Transaction:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """
                INSERT INTO transactions (
                    ticket_no, barcode, status, entry_lane, entry_time,
                    entry_plate, entry_confidence, entry_image_url,
                    flagged, flag_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_no,
                    barcode,
                    STATUS_PENDING,
                    lane,
                    entry_time,
                    plate,
                    confidence,
                    image_url,
                    int(flagged),
                    flag_reason,
                    now,
                    now,
                ),
            )
            self._conn.commit()
            row_id = int(cur.lastrowid)
        found = self.get(row_id)
        assert found is not None  # just inserted
        return found

    def get(self, transaction_id: int) -> Transaction | None:
        return self._fetch_one("SELECT * FROM transactions WHERE id = ?", (transaction_id,))

    def get_by_barcode(self, barcode: str) -> Transaction | None:
        return self._fetch_one(
            "SELECT * FROM transactions WHERE barcode = ?", (barcode,)
        )

    def get_by_ticket(self, ticket_no: str) -> Transaction | None:
        return self._fetch_one(
            "SELECT * FROM transactions WHERE ticket_no = ?", (ticket_no,)
        )

    def find_active_by_plate(self, plate: str) -> Transaction | None:
        """Used when the driver has lost the ticket but the plate was read."""
        return self._fetch_one(
            "SELECT * FROM transactions WHERE entry_plate = ? AND status IN (?, ?) "
            "ORDER BY entry_time DESC LIMIT 1",
            (plate, STATUS_ACTIVE, STATUS_AWAITING_PAYMENT),
        )

    def latest_entry_for_lane(self, lane: str) -> Transaction | None:
        """Backs the button debounce: the last ticket issued on this lane."""
        return self._fetch_one(
            "SELECT * FROM transactions WHERE entry_lane = ? "
            "ORDER BY id DESC LIMIT 1",
            (lane,),
        )

    def list_transactions(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[Transaction]:
        sql = "SELECT * FROM transactions"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Transaction.from_row(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM transactions GROUP BY status"
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def update(self, transaction_id: int, **fields: Any) -> Transaction:
        """Update named columns. Booleans are stored as 0/1."""
        if not fields:
            found = self.get(transaction_id)
            if found is None:
                raise KeyError(f"no transaction {transaction_id}")
            return found

        cleaned = {
            key: int(value) if isinstance(value, bool) else value
            for key, value in fields.items()
        }
        cleaned["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in cleaned)

        with self._lock:
            self._conn.execute(
                f"UPDATE transactions SET {assignments} WHERE id = ?",
                (*cleaned.values(), transaction_id),
            )
            self._conn.commit()

        found = self.get(transaction_id)
        if found is None:
            raise KeyError(f"no transaction {transaction_id}")
        return found

    # -- events ------------------------------------------------------------

    def log_event(
        self,
        *,
        source: str,
        type: str,
        lane: str | None = None,
        transaction_id: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (transaction_id, ts, lane, source, type, detail) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    transaction_id,
                    _now(),
                    lane,
                    source,
                    type,
                    json.dumps(detail, separators=(",", ":")) if detail else None,
                ),
            )
            self._conn.commit()

    def list_events(
        self, *, transaction_id: int | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if transaction_id is not None:
            sql += " WHERE transaction_id = ?"
            params.append(transaction_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "id": row["id"],
                "transaction_id": row["transaction_id"],
                "ts": row["ts"],
                "lane": row["lane"],
                "source": row["source"],
                "type": row["type"],
                "detail": json.loads(row["detail"]) if row["detail"] else None,
            }
            for row in rows
        ]

    # -- internals ---------------------------------------------------------

    def _fetch_one(self, sql: str, params: Iterable[Any]) -> Transaction | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return Transaction.from_row(row) if row else None
