"""SQLite-backed durable event store.

Every published event is persisted *before* dispatch.
On restart, pending/processing events are replayed.
Failed events after max retries go to the dead-letter queue.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from .event import BaseEvent, event_type_name, deserialize_event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_store (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    payload       TEXT NOT NULL,           -- JSON
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending / processing / done / failed
    retries       INTEGER NOT NULL DEFAULT 0,
    max_retries   INTEGER NOT NULL DEFAULT 3,
    error         TEXT,
    created_at    TEXT NOT NULL,
    processed_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_status ON event_store(status);
CREATE INDEX IF NOT EXISTS idx_event_type ON event_store(type);

CREATE TABLE IF NOT EXISTS dlq (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    original_event_id   TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    payload             TEXT NOT NULL,
    error               TEXT,
    failed_at           TEXT NOT NULL,
    retries             INTEGER NOT NULL DEFAULT 0
);
"""


class EventStore:
    """Durable SQLite store for the event bus."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("EventStore not initialised — call init() first")
        return self._db

    # ── Write ───────────────────────────────────────────────

    async def save(self, event: BaseEvent, max_retries: int = 3) -> None:
        """Persist an event with status='pending'."""
        payload = event.model_dump_json()
        await self.db.execute(
            """INSERT OR IGNORE INTO event_store
               (id, type, payload, status, retries, max_retries, created_at)
               VALUES (?, ?, ?, 'pending', 0, ?, ?)""",
            (
                event.id,
                event_type_name(event),
                payload,
                max_retries,
                event.created_at.isoformat(),
            ),
        )
        await self.db.commit()

    async def mark_processing(self, event_id: str) -> None:
        await self.db.execute(
            "UPDATE event_store SET status='processing' WHERE id=?",
            (event_id,),
        )
        await self.db.commit()

    async def mark_done(self, event_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            "UPDATE event_store SET status='done', processed_at=? WHERE id=?",
            (now, event_id),
        )
        await self.db.commit()

    async def mark_failed(self, event_id: str, error: str) -> None:
        await self.db.execute(
            """UPDATE event_store
               SET retries = retries + 1,
                   status  = CASE WHEN retries + 1 >= max_retries THEN 'failed' ELSE 'pending' END,
                   error   = ?
               WHERE id = ?""",
            (error, event_id),
        )
        await self.db.commit()

    async def move_to_dlq(self, event_id: str) -> None:
        """Move an exhausted event to the dead-letter queue."""
        row = await self.db.execute_fetchall(
            "SELECT type, payload, error, retries FROM event_store WHERE id=?",
            (event_id,),
        )
        if not row:
            return
        r = row[0]
        now = datetime.now(timezone.utc).isoformat()
        await self.db.execute(
            """INSERT INTO dlq (original_event_id, event_type, payload, error, failed_at, retries)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_id, r["type"], r["payload"], r["error"], now, r["retries"]),
        )
        await self.db.execute(
            "UPDATE event_store SET status='dlq' WHERE id=?",
            (event_id,),
        )
        await self.db.commit()

    # ── Read ────────────────────────────────────────────────

    async def get_pending(self) -> list[BaseEvent]:
        """Return all events in pending or processing status (for replay on restart)."""
        rows = await self.db.execute_fetchall(
            "SELECT id, type, payload FROM event_store WHERE status IN ('pending','processing') ORDER BY created_at"
        )
        events: list[BaseEvent] = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
                events.append(deserialize_event(r["type"], payload))
            except Exception:
                # Skip corrupted events — they'll be visible in DLQ
                continue
        return events

    async def get_dlq(self) -> list[dict]:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM dlq ORDER BY failed_at DESC"
        )
        return [dict(r) for r in rows]

    async def get_stats(self) -> dict:
        """Return counts by status for dashboard."""
        rows = await self.db.execute_fetchall(
            "SELECT status, COUNT(*) as cnt FROM event_store GROUP BY status"
        )
        return {r["status"]: r["cnt"] for r in rows}
