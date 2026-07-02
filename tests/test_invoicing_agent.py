"""Tests for the InvoicingAgent.

Tests cover:
- Invoice generation on booking
- Late cancellation invoice adjustment (50%)
- Full refund cancellation (0, paid)
- Payment confirmation (human marks as paid)
- Invoice stats
- Overdue invoice check (1st reminder, 2nd reminder + escalation)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.agents.invoicing import InvoicingAgent
from src.router.router import EventRouter
from src.router.event import (
    CancellationConfirmed,
    HumanApprovalRequired,
    InvoiceGenerated,
    PaymentConfirmed,
    PaymentReminder,
    ScheduleConfirmed,
    WalkCompleted,
)
from src.router.store import EventStore
from src.db.database import init_database, close_database
from src.db.seed import generate_seed_data, WALKERS
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
    return Settings(db_path=tmp_db_path)


def _track(router):
    emitted = []
    original = router.publish
    async def tracker(event):
        emitted.append(event)
        await original(event)
    router.publish = tracker
    return emitted


async def _create_walk_in_db(db, client_email="lisa.mueller@example.com", dog_name="Bello"):
    """Helper: create a walk in the DB and return (walk_id, client_id)."""
    from uuid import uuid4
    now = datetime.now(timezone.utc).isoformat()

    client = await db.execute_fetchall("SELECT * FROM clients WHERE email = ?", (client_email,))
    client_id = client[0]["id"]

    dog = await db.execute_fetchall("SELECT * FROM dogs WHERE client_id = ? AND name = ?", (client_id, dog_name))
    dog_id = dog[0]["id"]

    walker = await db.execute_fetchall("SELECT * FROM walkers WHERE active = 1 LIMIT 1")

    walk_id = uuid4().hex
    await db.execute(
        """INSERT INTO walks (id, client_id, dog_id, walker_id, date, slot, duration, status, price_eur, created_at)
           VALUES (?, ?, ?, ?, '2025-07-04', '11:30', 60, 'scheduled', 20.0, ?)""",
        (walk_id, client_id, dog_id, walker[0]["id"], now),
    )
    await db.commit()
    return walk_id, client_id


class TestInvoiceGeneration:
    async def test_booking_creates_invoice(self, setup_system, settings):
        router, db = setup_system
        agent = InvoicingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        walk_id, _ = await _create_walk_in_db(db)

        await router.publish(ScheduleConfirmed(
            booking_id=walk_id,
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            dog_name="Bello",
            walker_id="w1",
            walker_name="Sarah",
            walk_date="2025-07-04",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        invoices_emitted = [e for e in emitted if isinstance(e, InvoiceGenerated)]
        assert len(invoices_emitted) == 1
        assert invoices_emitted[0].amount_eur == 20.0

        # Check DB
        invoices = await db.execute_fetchall("SELECT * FROM invoices WHERE walk_id = ?", (walk_id,))
        assert len(invoices) == 1
        assert invoices[0]["status"] == "pending"
        assert invoices[0]["amount_eur"] == 20.0

        await agent.stop()

    async def test_invoice_due_date_7_days(self, setup_system, settings):
        router, db = setup_system
        agent = InvoicingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        walk_id, _ = await _create_walk_in_db(db)

        await router.publish(ScheduleConfirmed(
            booking_id=walk_id,
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            dog_name="Bello",
            walker_id="w1",
            walker_name="Sarah",
            walk_date="2025-07-04",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        invoices_emitted = [e for e in emitted if isinstance(e, InvoiceGenerated)]
        assert len(invoices_emitted) == 1
        # Due date should be ~7 days from now
        from datetime import date
        expected_due = (datetime.now(timezone.utc) + timedelta(days=7)).date().isoformat()
        assert invoices_emitted[0].due_date == expected_due

        await agent.stop()


class TestCancellationInvoice:
    async def test_late_cancellation_50_percent(self, setup_system, settings):
        router, db = setup_system
        agent = InvoicingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        walk_id, _ = await _create_walk_in_db(db)

        # First create the invoice
        await router.publish(ScheduleConfirmed(
            booking_id=walk_id,
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            dog_name="Bello",
            walker_id="w1",
            walker_name="Sarah",
            walk_date="2025-07-04",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        # Now cancel late
        await router.publish(CancellationConfirmed(
            booking_id=walk_id,
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            refund_percent=50,
            late_cancellation=True,
        ))
        await asyncio.sleep(0.3)

        # Invoice should be adjusted to 50%
        invoices = await db.execute_fetchall("SELECT * FROM invoices WHERE walk_id = ?", (walk_id,))
        assert len(invoices) == 1
        assert invoices[0]["amount_eur"] == 10.0  # 50% of 20

        # Should emit an InvoiceGenerated with adjusted amount
        invoice_events = [e for e in emitted if isinstance(e, InvoiceGenerated)]
        assert len(invoice_events) == 2  # original + adjusted
        assert invoice_events[1].amount_eur == 10.0

        await agent.stop()

    async def test_full_refund_cancellation_zeros_invoice(self, setup_system, settings):
        router, db = setup_system
        agent = InvoicingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        walk_id, _ = await _create_walk_in_db(db)

        # Create invoice
        await router.publish(ScheduleConfirmed(
            booking_id=walk_id,
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            dog_name="Bello",
            walker_id="w1",
            walker_name="Sarah",
            walk_date="2025-07-04",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        # Cancel with full refund
        await router.publish(CancellationConfirmed(
            booking_id=walk_id,
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            refund_percent=100,
            late_cancellation=False,
        ))
        await asyncio.sleep(0.3)

        invoices = await db.execute_fetchall("SELECT * FROM invoices WHERE walk_id = ?", (walk_id,))
        assert len(invoices) == 1
        assert invoices[0]["amount_eur"] == 0
        assert invoices[0]["status"] == "paid"

        await agent.stop()


class TestPaymentConfirmation:
    async def test_human_marks_payment_as_paid(self, setup_system, settings):
        router, db = setup_system
        agent = InvoicingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        walk_id, _ = await _create_walk_in_db(db)

        # Create invoice
        await router.publish(ScheduleConfirmed(
            booking_id=walk_id,
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            dog_name="Bello",
            walker_id="w1",
            walker_name="Sarah",
            walk_date="2025-07-04",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        invoice = await db.execute_fetchall("SELECT * FROM invoices WHERE walk_id = ?", (walk_id,))
        invoice_id = invoice[0]["id"]

        # Human marks as paid
        await router.publish(PaymentConfirmed(
            invoice_id=invoice_id,
            client_email="lisa.mueller@example.com",
            amount_eur=20.0,
        ))
        await asyncio.sleep(0.3)

        invoices = await db.execute_fetchall("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
        assert invoices[0]["status"] == "paid"
        assert invoices[0]["paid_date"] is not None

        await agent.stop()


class TestOverdueCheck:
    async def test_first_reminder_after_7_days(self, setup_system, settings):
        router, db = setup_system
        agent = InvoicingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Create an invoice with an old created_at (8 days ago)
        from uuid import uuid4
        old_date = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        walk_id, client_id = await _create_walk_in_db(db)

        invoice_id = uuid4().hex
        await db.execute(
            """INSERT INTO invoices (id, client_id, walk_id, amount_eur, status, due_date, created_at)
               VALUES (?, ?, ?, 20.0, 'pending', '2025-01-01', ?)""",
            (invoice_id, client_id, walk_id, old_date),
        )
        await db.commit()

        # Check overdue
        overdue = await agent.check_overdue_invoices()
        assert len(overdue) == 1

        # Should emit a PaymentReminder (1st)
        reminders = [e for e in emitted if isinstance(e, PaymentReminder)]
        assert len(reminders) == 1
        assert reminders[0].reminder_count == 1

        await agent.stop()

    async def test_second_reminder_and_escalation_after_14_days(self, setup_system, settings):
        router, db = setup_system
        agent = InvoicingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        from uuid import uuid4
        old_date = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        walk_id, client_id = await _create_walk_in_db(db)

        invoice_id = uuid4().hex
        await db.execute(
            """INSERT INTO invoices (id, client_id, walk_id, amount_eur, status, due_date, created_at)
               VALUES (?, ?, ?, 20.0, 'pending', '2025-01-01', ?)""",
            (invoice_id, client_id, walk_id, old_date),
        )
        await db.commit()

        overdue = await agent.check_overdue_invoices()
        assert len(overdue) == 1

        # Should emit 2nd reminder
        reminders = [e for e in emitted if isinstance(e, PaymentReminder)]
        assert len(reminders) == 1
        assert reminders[0].reminder_count == 2

        # Should emit human escalation
        gates = [e for e in emitted if isinstance(e, HumanApprovalRequired)]
        assert len(gates) == 1
        assert gates[0].gate_type == "payment_escalation"

        await agent.stop()

    async def test_recent_invoice_not_reminded(self, setup_system, settings):
        router, db = setup_system
        agent = InvoicingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        from uuid import uuid4
        walk_id, client_id = await _create_walk_in_db(db)

        # Invoice created 2 days ago (not overdue)
        recent_date = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        invoice_id = uuid4().hex
        await db.execute(
            """INSERT INTO invoices (id, client_id, walk_id, amount_eur, status, due_date, created_at)
               VALUES (?, ?, ?, 20.0, 'pending', '2025-01-01', ?)""",
            (invoice_id, client_id, walk_id, recent_date),
        )
        await db.commit()

        overdue = await agent.check_overdue_invoices()
        assert len(overdue) == 0

        reminders = [e for e in emitted if isinstance(e, PaymentReminder)]
        assert len(reminders) == 0

        await agent.stop()


class TestInvoiceStats:
    async def test_get_invoice_stats(self, setup_system, settings):
        router, db = setup_system
        agent = InvoicingAgent(router, settings)
        await agent.start()

        walk_id, _ = await _create_walk_in_db(db)

        await router.publish(ScheduleConfirmed(
            booking_id=walk_id,
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            dog_name="Bello",
            walker_id="w1",
            walker_name="Sarah",
            walk_date="2025-07-04",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        stats = await agent.get_invoice_stats()
        assert "pending" in stats
        assert stats["pending"]["count"] == 1
        assert stats["pending"]["total"] == 20.0

        await agent.stop()
