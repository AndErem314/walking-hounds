"""Tests for LoggerAgent — the audit trail agent."""

import asyncio
import json
import pytest

from src.router.event import (
    BookingIntent,
    ScheduleConfirmed,
    CancellationConfirmed,
    InvoiceGenerated,
    HumanApprovalRequired,
    EmailReceived,
)
from src.router.store import EventStore
from src.router.router import EventRouter
from src.agents.logger import LoggerAgent
from src.db.database import init_database, close_database


@pytest.fixture
async def setup_system(tmp_path):
    """Set up a full test system with router + logger agent."""
    db_path = str(tmp_path / "test.db")
    db = await init_database(db_path)

    store = EventStore(db_path)
    await store.init()
    router = EventRouter(store)

    logger_agent = LoggerAgent(router)
    await router.start()
    await logger_agent.start()

    yield router, logger_agent, db

    await logger_agent.stop()
    await router.stop()
    await store.close()
    await close_database(db)


class TestLoggerAgent:

    async def test_logger_receives_wildcard(self, setup_system):
        """Logger should subscribe to all events (wildcard)."""
        router, logger_agent, db = setup_system

        event_types = logger_agent.subscribed_event_types()
        assert "*" in event_types

    async def test_logger_records_booking_intent(self, setup_system):
        """Logger should record a BookingIntent event."""
        router, logger_agent, db = setup_system

        await router.publish(BookingIntent(
            client_email="test@example.com",
            client_name="Test Client",
            dog_name="Rex",
            walk_date="2026-07-15",
            walk_slot="11:30",
        ))

        await asyncio.sleep(0.3)

        rows = await db.execute_fetchall("SELECT * FROM journal")
        assert len(rows) == 1
        entry = dict(rows[0])
        assert entry["event_type"] == "BookingIntent"
        assert entry["actor"] == "IntakeAgent"
        details = json.loads(entry["details"])
        assert details["client_email"] == "test@example.com"
        assert details["dog_name"] == "Rex"

    async def test_logger_records_schedule_confirmed(self, setup_system):
        """Logger should record a ScheduleConfirmed event."""
        router, logger_agent, db = setup_system

        await router.publish(ScheduleConfirmed(
            booking_id="walk-123",
            client_email="client@example.com",
            client_name="Jane",
            dog_name="Buddy",
            walker_id="walker-1",
            walker_name="Alice",
            walk_date="2026-07-15",
            walk_slot="12:00",
        ))

        await asyncio.sleep(0.3)

        rows = await db.execute_fetchall("SELECT * FROM journal")
        assert len(rows) == 1
        entry = dict(rows[0])
        assert entry["event_type"] == "ScheduleConfirmed"
        assert entry["actor"] == "SchedulingAgent"
        assert entry["related_booking_id"] == "walk-123"
        assert entry["related_client_id"] == "client@example.com"

    async def test_logger_records_cancellation(self, setup_system):
        """Logger should record a CancellationConfirmed event."""
        router, logger_agent, db = setup_system

        await router.publish(CancellationConfirmed(
            booking_id="walk-456",
            client_email="cancel@example.com",
            client_name="John",
            refund_percent=50,
            late_cancellation=True,
        ))

        await asyncio.sleep(0.3)

        rows = await db.execute_fetchall("SELECT * FROM journal")
        assert len(rows) == 1
        entry = dict(rows[0])
        assert entry["event_type"] == "CancellationConfirmed"
        assert entry["actor"] == "SchedulingAgent"

    async def test_logger_records_invoice(self, setup_system):
        """Logger should record an InvoiceGenerated event."""
        router, logger_agent, db = setup_system

        await router.publish(InvoiceGenerated(
            invoice_id="inv-789",
            client_email="pay@example.com",
            client_name="Pay Client",
            amount_eur=20.0,
            due_date="2026-07-22",
            walk_date="2026-07-15",
            booking_id="walk-789",
        ))

        await asyncio.sleep(0.3)

        rows = await db.execute_fetchall("SELECT * FROM journal")
        assert len(rows) == 1
        entry = dict(rows[0])
        assert entry["event_type"] == "InvoiceGenerated"
        assert entry["actor"] == "InvoicingAgent"

    async def test_logger_records_human_gate(self, setup_system):
        """Logger should record a HumanApprovalRequired event."""
        router, logger_agent, db = setup_system

        await router.publish(HumanApprovalRequired(
            gate_type="complaint_response",
            context={"client_email": "angry@example.com", "complaint": "bad service"},
            options=["approve", "edit", "reject"],
        ))

        await asyncio.sleep(0.3)

        rows = await db.execute_fetchall("SELECT * FROM journal")
        assert len(rows) == 1
        entry = dict(rows[0])
        assert entry["event_type"] == "HumanApprovalRequired"
        assert entry["actor"] == "System"

    async def test_logger_records_multiple_events(self, setup_system):
        """Logger should record multiple events in sequence."""
        router, logger_agent, db = setup_system

        for i in range(5):
            await router.publish(BookingIntent(
                client_email=f"client{i}@example.com",
                client_name=f"Client {i}",
                dog_name=f"Dog{i}",
                walk_date="2026-07-15",
                walk_slot="11:30",
            ))

        await asyncio.sleep(0.5)

        rows = await db.execute_fetchall("SELECT * FROM journal ORDER BY timestamp")
        assert len(rows) == 5
        for i, row in enumerate(rows):
            entry = dict(row)
            assert entry["event_type"] == "BookingIntent"
            details = json.loads(entry["details"])
            assert details["client_email"] == f"client{i}@example.com"

    async def test_logger_records_email_received(self, setup_system):
        """Logger should record an EmailReceived event."""
        router, logger_agent, db = setup_system

        from datetime import datetime, timezone

        await router.publish(EmailReceived(
            message_id="msg-001",
            from_email="sender@example.com",
            subject="Book a walk",
            body="I'd like to book a walk for my dog",
            received_at=datetime.now(timezone.utc),
        ))

        await asyncio.sleep(0.3)

        rows = await db.execute_fetchall("SELECT * FROM journal")
        assert len(rows) == 1
        entry = dict(rows[0])
        assert entry["event_type"] == "EmailReceived"
        assert entry["actor"] == "IMAP"

    async def test_get_recent_entries(self, setup_system):
        """Logger should return recent journal entries."""
        router, logger_agent, db = setup_system

        for i in range(10):
            await router.publish(BookingIntent(
                client_email=f"c{i}@example.com",
                dog_name=f"Dog{i}",
                walk_date="2026-07-15",
            ))

        await asyncio.sleep(0.5)

        entries = await logger_agent.get_recent_entries(limit=5)
        assert len(entries) == 5
        # Should be ordered by timestamp DESC
        assert entries[0]["event_type"] == "BookingIntent"

    async def test_get_entry_count(self, setup_system):
        """Logger should return the correct entry count."""
        router, logger_agent, db = setup_system

        for i in range(3):
            await router.publish(BookingIntent(
                client_email=f"count{i}@example.com",
                dog_name=f"Count{i}",
                walk_date="2026-07-15",
            ))

        await asyncio.sleep(0.5)

        count = await logger_agent.get_entry_count()
        assert count == 3

    async def test_logger_details_are_json(self, setup_system):
        """Journal details should be valid JSON."""
        router, logger_agent, db = setup_system

        await router.publish(ScheduleConfirmed(
            booking_id="json-test",
            client_email="json@example.com",
            client_name="JSON",
            dog_name="TestDog",
            walker_id="w1",
            walker_name="Walker1",
            walk_date="2026-07-15",
            walk_slot="11:30",
        ))

        await asyncio.sleep(0.3)

        rows = await db.execute_fetchall("SELECT details FROM journal")
        assert len(rows) == 1
        details = json.loads(rows[0]["details"])
        assert isinstance(details, dict)
        assert details["dog_name"] == "TestDog"
        assert details["walk_date"] == "2026-07-15"
