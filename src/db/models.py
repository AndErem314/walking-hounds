"""Convenience functions for common DB queries.

These are thin async wrappers around aiosqlite — no ORM, just SQL.
Used by agents and the dashboard API.
"""

from __future__ import annotations

from typing import Any

import aiosqlite


async def fetch_all(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> list[dict]:
    rows = await db.execute_fetchall(sql, params)
    return [dict(r) for r in rows]


async def fetch_one(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> dict | None:
    rows = await db.execute_fetchall(sql, params)
    return dict(rows[0]) if rows else None


async def execute(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> None:
    await db.execute(sql, params)
    await db.commit()


async def journal_entry(
    db: aiosqlite.Connection,
    *,
    event_type: str,
    actor: str,
    details: dict[str, Any],
    related_booking_id: str | None = None,
    related_client_id: str | None = None,
) -> None:
    import json
    from datetime import datetime, timezone
    from uuid import uuid4

    await execute(
        db,
        """INSERT INTO journal (id, event_type, timestamp, actor, details, related_booking_id, related_client_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            uuid4().hex,
            event_type,
            datetime.now(timezone.utc).isoformat(),
            actor,
            json.dumps(details),
            related_booking_id,
            related_client_id,
        ),
    )
