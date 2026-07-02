"""Tests for the ReminderAgent.

Tests cover:
- Walk reminder triggers (within reminder window)
- Walk completion detection (past slot time)
- Reminder dedup (doesn't send twice)
- Walker morning briefing logic
- DateTime parsing helper
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.agents.reminder import ReminderAgent
from src.router.router import EventRouter
from src.router.event import (
    ReminderDue,
    WalkCompleted,
)
from src.router.store import EventStore
from src.db.database import init_database, close_database
from src.db.seed import generate_seed_data
from src.config import Settings


@pytest.fixture
async def setup_system(tmp_db_path):
    db = await init_database(tmp_db_path)
    await generate_seed_data(db)

    store = EventStore(tmp_db_path)
    await store.init()
    router = EventRouter(store)
    await router.start()

    yield router, db

    await router.stop()
    await store.close()
    await close_database(db)


@pytest.fixture
def settings(tmp_db_path):
    return Settings(db_path=tmp_db_path, reminder_poll_interval_sec=1)


def _track(router):
    emitted = []
    original = router.publish
    async def tracker(event):
        emitted.append(event)
        await original(event)
    router.publish = tracker
    return emitted


async def _create_walk_at_time(db, client_email, dog_name, walk_dt):
    """Create a walk in the DB at a specific datetime."""
    now = datetime.now(timezone.utc).isoformat()
    client = await db.execute_fetchall("SELECT * FROM clients WHERE email = ?", (client_email,))
    client_id = client[0]["id"]
    dog = await db.execute_fetchall("SELECT * FROM dogs WHERE client_id = ? AND name = ?", (client_id, dog_name))
    walker = await db.execute_fetchall("SELECT * FROM walkers WHERE active = 1 LIMIT 1")

    walk_id = uuid4().hex
    date_str = walk_dt.date().isoformat()
    slot_str = f"{walk_dt.hour:02d}:{walk_dt.minute:02d}"

    await db.execute(
        """INSERT INTO walks (id, client_id, dog_id, walker_id, date, slot, duration, status, price_eur, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 60, 'scheduled', 20.0, ?)""",
        (walk_id, client_id, dog[0]["id"], walker[0]["id"], date_str, slot_str, now),
    )
    await db.commit()
    return walk_id


class TestWalkReminders:
    async def test_reminder_emitted_for_upcoming_walk(self, setup_system, settings):
        """Walk starting in 1 hour (within 2h window) → reminder emitted."""
        router, db = setup_system
        agent = ReminderAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Create a walk starting in 1 hour
        walk_time = datetime.now(timezone.utc) + timedelta(hours=1)
        walk_id = await _create_walk_at_time(db, "lisa.mueller@example.com", "Bello", walk_time)

        # Trigger the check manually
        await agent._check_walk_reminders()
        await asyncio.sleep(0.3)

        reminders = [e for e in emitted if isinstance(e, ReminderDue) and e.reminder_type == "walk_reminder"]
        assert len(reminders) == 1
        assert reminders[0].booking_id == walk_id
        assert reminders[0].target_email == "lisa.mueller@example.com"

        await agent.stop()

    async def test_no_reminder_for_far_future_walk(self, setup_system, settings):
        """Walk starting in 5 hours (outside 2h window) → no reminder."""
        router, db = setup_system
        agent = ReminderAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        walk_time = datetime.now(timezone.utc) + timedelta(hours=5)
        await _create_walk_at_time(db, "lisa.mueller@example.com", "Bello", walk_time)

        await agent._check_walk_reminders()
        await asyncio.sleep(0.3)

        reminders = [e for e in emitted if isinstance(e, ReminderDue) and e.reminder_type == "walk_reminder"]
        assert len(reminders) == 0

        await agent.stop()

    async def test_reminder_not_sent_twice(self, setup_system, settings):
        """Same walk shouldn't get two reminders."""
        router, db = setup_system
        agent = ReminderAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        walk_time = datetime.now(timezone.utc) + timedelta(hours=1)
        walk_id = await _create_walk_at_time(db, "lisa.mueller@example.com", "Bello", walk_time)

        # Check twice
        await agent._check_walk_reminders()
        await asyncio.sleep(0.2)
        await agent._check_walk_reminders()
        await asyncio.sleep(0.2)

        reminders = [e for e in emitted if isinstance(e, ReminderDue) and e.reminder_type == "walk_reminder"]
        assert len(reminders) == 1  # only once

        await agent.stop()


class TestWalkCompletion:
    async def test_walk_completed_after_slot_time(self, setup_system, settings):
        """Walk whose slot has passed → WalkCompleted + feedback request."""
        router, db = setup_system
        agent = ReminderAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Create a walk that started 2 hours ago (well past the 60-min duration)
        walk_time = datetime.now(timezone.utc) - timedelta(hours=2)
        walk_id = await _create_walk_at_time(db, "lisa.mueller@example.com", "Bello", walk_time)

        await agent._check_walk_completions()
        await asyncio.sleep(0.3)

        completed = [e for e in emitted if isinstance(e, WalkCompleted)]
        assert len(completed) == 1
        assert completed[0].booking_id == walk_id
        assert completed[0].dog_name == "Bello"

        # Should also emit a feedback request
        feedback = [e for e in emitted if isinstance(e, ReminderDue) and e.reminder_type == "feedback"]
        assert len(feedback) == 1

        # Walk should be marked completed in DB
        walks = await db.execute_fetchall("SELECT * FROM walks WHERE id = ?", (walk_id,))
        assert walks[0]["status"] == "completed"

        await agent.stop()

    async def test_future_walk_not_completed(self, setup_system, settings):
        router, db = setup_system
        agent = ReminderAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        walk_time = datetime.now(timezone.utc) + timedelta(hours=3)
        await _create_walk_at_time(db, "lisa.mueller@example.com", "Bello", walk_time)

        await agent._check_walk_completions()
        await asyncio.sleep(0.3)

        completed = [e for e in emitted if isinstance(e, WalkCompleted)]
        assert len(completed) == 0

        await agent.stop()


class TestReminderHelpers:
    def test_parse_walk_datetime_valid(self):
        dt = ReminderAgent._parse_walk_datetime("2025-07-04", "11:30")
        assert dt is not None
        assert dt.hour == 11
        assert dt.minute == 30

    def test_parse_walk_datetime_invalid_date(self):
        dt = ReminderAgent._parse_walk_datetime("invalid", "11:30")
        assert dt is None

    def test_parse_walk_datetime_invalid_slot(self):
        dt = ReminderAgent._parse_walk_datetime("2025-07-04", "invalid")
        assert dt is None

    def test_parse_walk_datetime_none_inputs(self):
        dt = ReminderAgent._parse_walk_datetime(None, None)
        assert dt is None
