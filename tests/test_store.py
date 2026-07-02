"""Tests for the SQLite-backed EventStore."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.bus.event import BookingIntent, EmailReceived, event_type_name
from src.bus.store import EventStore


@pytest.fixture
async def store(tmp_db_path):
    s = EventStore(tmp_db_path)
    await s.init()
    yield s
    await s.close()


class TestEventStoreInit:
    async def test_init_creates_tables(self, tmp_db_path):
        s = EventStore(tmp_db_path)
        await s.init()
        # Check event_store table exists
        rows = await s.db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('event_store','dlq')"
        )
        table_names = {r["name"] for r in rows}
        assert "event_store" in table_names
        assert "dlq" in table_names
        await s.close()

    async def test_wal_mode_enabled(self, tmp_db_path):
        s = EventStore(tmp_db_path)
        await s.init()
        rows = await s.db.execute_fetchall("PRAGMA journal_mode")
        assert rows[0][0].lower() == "wal"
        await s.close()


class TestEventStoreSave:
    async def test_save_event(self, store):
        ev = BookingIntent(client_email="test@example.com", confidence=0.9)
        await store.save(ev)
        rows = await store.db.execute_fetchall(
            "SELECT id, type, status FROM event_store WHERE id=?", (ev.id,)
        )
        assert len(rows) == 1
        assert rows[0]["type"] == "BookingIntent"
        assert rows[0]["status"] == "pending"

    async def test_save_is_idempotent(self, store):
        ev = BookingIntent(client_email="test@example.com")
        await store.save(ev)
        # Saving again with same id should be ignored (INSERT OR IGNORE)
        await store.save(ev)
        rows = await store.db.execute_fetchall(
            "SELECT COUNT(*) as cnt FROM event_store WHERE id=?", (ev.id,)
        )
        assert rows[0]["cnt"] == 1

    async def test_save_preserves_payload(self, store):
        ev = BookingIntent(
            client_email="test@example.com",
            client_name="Lisa",
            dog_name="Bello",
            walk_date="2025-07-04",
            confidence=0.92,
        )
        await store.save(ev)
        rows = await store.db.execute_fetchall(
            "SELECT payload FROM event_store WHERE id=?", (ev.id,)
        )
        import json
        payload = json.loads(rows[0]["payload"])
        assert payload["client_email"] == "test@example.com"
        assert payload["dog_name"] == "Bello"
        assert payload["confidence"] == 0.92


class TestEventStoreStatus:
    async def test_mark_processing(self, store):
        ev = BookingIntent(client_email="a@b.com")
        await store.save(ev)
        await store.mark_processing(ev.id)
        rows = await store.db.execute_fetchall(
            "SELECT status FROM event_store WHERE id=?", (ev.id,)
        )
        assert rows[0]["status"] == "processing"

    async def test_mark_done(self, store):
        ev = BookingIntent(client_email="a@b.com")
        await store.save(ev)
        await store.mark_processing(ev.id)
        await store.mark_done(ev.id)
        rows = await store.db.execute_fetchall(
            "SELECT status, processed_at FROM event_store WHERE id=?", (ev.id,)
        )
        assert rows[0]["status"] == "done"
        assert rows[0]["processed_at"] is not None

    async def test_mark_failed_increments_retries(self, store):
        ev = BookingIntent(client_email="a@b.com")
        await store.save(ev, max_retries=3)
        await store.mark_failed(ev.id, "test error")
        rows = await store.db.execute_fetchall(
            "SELECT retries, status, error FROM event_store WHERE id=?", (ev.id,)
        )
        assert rows[0]["retries"] == 1
        assert rows[0]["status"] == "pending"  # still under max_retries
        assert "test error" in rows[0]["error"]

    async def test_mark_failed_sets_failed_after_max_retries(self, store):
        ev = BookingIntent(client_email="a@b.com")
        await store.save(ev, max_retries=2)
        await store.mark_failed(ev.id, "err1")
        await store.mark_failed(ev.id, "err2")
        rows = await store.db.execute_fetchall(
            "SELECT retries, status FROM event_store WHERE id=?", (ev.id,)
        )
        assert rows[0]["retries"] == 2
        assert rows[0]["status"] == "failed"


class TestEventStoreDLQ:
    async def test_move_to_dlq(self, store):
        ev = BookingIntent(client_email="a@b.com")
        await store.save(ev)
        await store.mark_failed(ev.id, "fatal error")
        await store.move_to_dlq(ev.id)

        # Event store should show 'dlq' status
        rows = await store.db.execute_fetchall(
            "SELECT status FROM event_store WHERE id=?", (ev.id,)
        )
        assert rows[0]["status"] == "dlq"

        # DLQ should have the entry
        dlq_items = await store.get_dlq()
        assert len(dlq_items) == 1
        assert dlq_items[0]["event_type"] == "BookingIntent"
        assert "fatal error" in dlq_items[0]["error"]

    async def test_dlq_empty_initially(self, store):
        items = await store.get_dlq()
        assert items == []


class TestEventStoreReplay:
    async def test_get_pending_returns_pending_and_processing(self, store):
        ev1 = BookingIntent(client_email="a@b.com")
        ev2 = BookingIntent(client_email="c@d.com")
        ev3 = BookingIntent(client_email="e@f.com")
        await store.save(ev1)
        await store.save(ev2)
        await store.save(ev3)

        await store.mark_processing(ev2.id)
        await store.mark_done(ev3.id)

        pending = await store.get_pending()
        pending_ids = {e.id for e in pending}
        assert ev1.id in pending_ids       # pending
        assert ev2.id in pending_ids       # processing
        assert ev3.id not in pending_ids   # done

    async def test_get_pending_preserves_type(self, store):
        ev = BookingIntent(client_email="a@b.com", dog_name="Bello")
        await store.save(ev)
        pending = await store.get_pending()
        assert len(pending) == 1
        assert isinstance(pending[0], BookingIntent)
        assert pending[0].dog_name == "Bello"

    async def test_get_pending_empty(self, store):
        pending = await store.get_pending()
        assert pending == []


class TestEventStoreStats:
    async def test_stats_counts(self, store):
        ev1 = BookingIntent(client_email="a@b.com")
        ev2 = BookingIntent(client_email="c@d.com")
        await store.save(ev1)
        await store.save(ev2)
        await store.mark_done(ev1.id)

        stats = await store.get_stats()
        assert stats.get("done") == 1
        assert stats.get("pending") == 1

    async def test_stats_empty(self, store):
        stats = await store.get_stats()
        assert stats == {} or all(v == 0 for v in stats.values())
