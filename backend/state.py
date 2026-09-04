"""Session finite-state machine, SQLite log, and SSE broadcaster."""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum

import aiosqlite

from .config import settings

log = logging.getLogger("state")


class State(str, Enum):
    IDLE = "IDLE"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    PAID = "PAID"
    SHOOTING = "SHOOTING"
    PRINTING = "PRINTING"
    DONE = "DONE"
    REFUNDING = "REFUNDING"
    REFUNDED = "REFUNDED"
    OUT_OF_SERVICE = "OUT_OF_SERVICE"


# States that mean "this invoice has already been acted on" (idempotency guard).
ACTIVE_STATES = {
    State.PAID,
    State.SHOOTING,
    State.PRINTING,
    State.DONE,
    State.REFUNDING,
    State.REFUNDED,
}


@dataclass
class Session:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    state: State = State.IDLE
    invoice_id: str = ""
    pay_url: str = ""
    amount_uah: int = 0
    created_at: float = field(default_factory=time.time)
    paid_at: float | None = None
    finished_at: float | None = None
    error: str = ""
    trigger_attempts: int = 0
    # transient, not persisted: when the current timed phase ends
    phase_deadline: float | None = None
    # transient SHOOTING sub-phase (driven by dslrBooth events, or by the
    # timed fallback). shoot_phase: "" | get_ready | counting | capture | processing
    shoot_phase: str = ""
    shot: int = 0                       # current shot, 1-based; 0 before the first countdown
    countdown_deadline: float | None = None   # when the current 3·2·1 reaches zero

    def new_cycle(self) -> None:
        """Reset to a fresh IDLE session, keeping nothing from the previous one."""
        fresh = Session()
        self.__dict__.update(fresh.__dict__)


# ──────────────────────────── storage ────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    booth_id      TEXT,
    state         TEXT NOT NULL,
    invoice_id    TEXT,
    amount_uah    INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL,
    paid_at       REAL,
    finished_at   REAL,
    error         TEXT DEFAULT '',
    prints        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_invoice ON sessions(invoice_id);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at);

CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);

-- money that must go back to a card but the refund call hasn't confirmed yet.
-- kind='watch'  : invoice abandoned on our side — poll it; if it turns out paid,
--                 flip to kind='refund' (guest already left, auto-return).
-- kind='refund' : refund owed — retry provider.refund() until 'reversed'.
CREATE TABLE IF NOT EXISTS pending_refunds (
    invoice_id  TEXT PRIMARY KEY,
    booth_id    TEXT,
    session_id  TEXT,
    amount_uah  INTEGER NOT NULL DEFAULT 0,
    reason      TEXT DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'refund',
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_try    REAL NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
"""


class SessionStore:
    def __init__(self, db_path: str | None = None) -> None:
        self._path = str(db_path or settings.db_file)

    async def init(self) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.executescript(_SCHEMA)
            # forward-compat: add booth_id column to pre-existing DBs
            cur = await db.execute("PRAGMA table_info(sessions)")
            cols = {row[1] for row in await cur.fetchall()}
            if "booth_id" not in cols:
                await db.execute("ALTER TABLE sessions ADD COLUMN booth_id TEXT")
            await db.commit()

    async def save(self, s: Session, *, prints: int = 0) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """
                INSERT INTO sessions (id, booth_id, state, invoice_id, amount_uah,
                                      created_at, paid_at, finished_at, error, prints)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    state=excluded.state,
                    invoice_id=excluded.invoice_id,
                    amount_uah=excluded.amount_uah,
                    paid_at=excluded.paid_at,
                    finished_at=excluded.finished_at,
                    error=excluded.error,
                    prints=MAX(sessions.prints, excluded.prints)
                """,
                (
                    s.id, settings.booth_id, s.state.value, s.invoice_id, s.amount_uah,
                    s.created_at, s.paid_at, s.finished_at, s.error, prints,
                ),
            )
            await db.commit()

    # ── key/value meta (paper counter, pause flag, …) ──
    async def get_meta(self, key: str, default: str = "") -> str:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute("SELECT v FROM kv WHERE k = ?", (key,))
            row = await cur.fetchone()
            return row[0] if row else default

    async def set_meta(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "INSERT INTO kv (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, str(value)),
            )
            await db.commit()

    async def total_prints(self) -> int:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute("SELECT COALESCE(SUM(prints), 0) FROM sessions")
            return int((await cur.fetchone())[0])

    async def load_last(self) -> dict | None:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM sessions ORDER BY created_at DESC LIMIT 1"
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def seen_invoice(self, invoice_id: str, exclude_id: str = "") -> bool:
        """True if some OTHER session row already acted on this invoice
        (idempotency guard against a duplicated PAID → run_session)."""
        if not invoice_id:
            return False
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                "SELECT 1 FROM sessions WHERE invoice_id = ? AND id != ? "
                "AND state != ? LIMIT 1",
                (invoice_id, exclude_id, State.IDLE.value),
            )
            return await cur.fetchone() is not None

    # ── pending refunds / abandoned-invoice watch ──────────────────
    async def enqueue_refund(self, *, invoice_id: str, session_id: str,
                             amount_uah: int, reason: str, kind: str = "refund",
                             next_try: float = 0.0) -> None:
        if not invoice_id:
            return
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                """INSERT INTO pending_refunds
                     (invoice_id, booth_id, session_id, amount_uah, reason, kind,
                      attempts, next_try, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                   ON CONFLICT(invoice_id) DO NOTHING""",
                (invoice_id, settings.booth_id, session_id, amount_uah, reason,
                 kind, next_try, time.time()),
            )
            await db.commit()

    async def due_pending(self, now: float) -> list[dict]:
        async with aiosqlite.connect(self._path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM pending_refunds WHERE next_try <= ? ORDER BY next_try",
                (now,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def update_pending(self, invoice_id: str, *, kind: str | None = None,
                             attempts: int | None = None,
                             next_try: float | None = None,
                             reason: str | None = None) -> None:
        sets, vals = [], []
        for col, v in (("kind", kind), ("attempts", attempts),
                       ("next_try", next_try), ("reason", reason)):
            if v is not None:
                sets.append(f"{col} = ?")
                vals.append(v)
        if not sets:
            return
        vals.append(invoice_id)
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                f"UPDATE pending_refunds SET {', '.join(sets)} WHERE invoice_id = ?",
                vals,
            )
            await db.commit()

    async def drop_pending(self, invoice_id: str) -> None:
        async with aiosqlite.connect(self._path) as db:
            await db.execute(
                "DELETE FROM pending_refunds WHERE invoice_id = ?", (invoice_id,)
            )
            await db.commit()

    async def pending_count(self) -> int:
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM pending_refunds")
            return int((await cur.fetchone())[0])

    async def stats_since(self, ts: float) -> tuple[int, int, int]:
        """(session_count, revenue_uah, prints) for DONE sessions since ts."""
        async with aiosqlite.connect(self._path) as db:
            cur = await db.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(amount_uah), 0), COALESCE(SUM(prints), 0)
                FROM sessions
                WHERE finished_at >= ? AND state = ?
                """,
                (ts, State.DONE.value),
            )
            row = await cur.fetchone()
            return int(row[0]), int(row[1]), int(row[2])


# ──────────────────────────── broadcaster ────────────────────────────
class Broadcaster:
    """Fan-out of state snapshots to all connected SSE clients."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[str]] = set()
        self._last: str = json.dumps({"state": State.IDLE.value})

    def publish(self, snapshot: dict) -> None:
        payload = json.dumps(snapshot, ensure_ascii=False)
        self._last = payload
        for q in list(self._subs):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:  # slow client — drop it, it will reconnect
                self._subs.discard(q)

    async def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=16)
        q.put_nowait(self._last)  # immediately hand the newcomer current state
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subs.discard(q)


# ──────────────────────────── snapshot ────────────────────────────
def snapshot(s: Session) -> dict:
    now = time.time()
    seconds_left = None
    if s.phase_deadline is not None:
        seconds_left = max(0, round(s.phase_deadline - now))
    snap = {
        "state": s.state.value,
        "price": settings.price_uah,
        "currency": settings.currency,
        "shots": settings.shots,
        "session_id": s.id,
        "seconds_left": seconds_left,
        "ts": now,
    }
    if s.state == State.AWAITING_PAYMENT:
        snap["pay_url"] = s.pay_url
        # qr_data_uri is injected by the controller (needs the qr module)
    if s.state == State.SHOOTING:
        snap["shoot_phase"] = s.shoot_phase or "get_ready"
        snap["shot"] = s.shot
        if s.shoot_phase == "counting" and s.countdown_deadline is not None:
            snap["countdown_ms_left"] = max(0, round((s.countdown_deadline - now) * 1000))
    return snap
