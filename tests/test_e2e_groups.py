"""E2E tests: multiple bookings, group composition, puppy groups, payment flow."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.router.event import (
    BookingIntent,
    PaymentConfirmed,
)
from src.router.store import EventStore
from src.router.router import EventRouter
from src.agents.scheduling import SchedulingAgent
from src.agents.communication import CommunicationAgent
from src.agents.invoicing import InvoicingAgent
from src.agents.logger import LoggerAgent
from src.db.database import init_database, close_database
from src.db.seed import generate_seed_data
from src.config import Settings


async def _drain(timeout: float = 1.0):
    await asyncio.sleep(timeout)


@pytest.fixture
async def e2e_system(tmp_path):
    db_path = str(tmp_path / "e2e3.db")
    db = await init_database(db_path)
    await generate_seed_data(db)

    store = EventStore(db_path)
    await store.init()
    router = EventRouter(store)

    settings = Settings(
        db_path=db_path,
        imap_host="", imap_user="", imap_password="",
        smtp_host="", smtp_user="test@walking-hounds.local", smtp_password="",
        ollama_host="http://localhost:11434", ollama_model="llama3.1:8b",
    )

    sched = SchedulingAgent(router, settings)
    comm = CommunicationAgent(router, settings)
    comm._smtp = MagicMock()
    comm._smtp.send = AsyncMock(return_value=True)
    comm._ollama = MagicMock()
    comm._ollama.generate = AsyncMock(return_value="Test response.")
    comm._ollama.init = AsyncMock()
    comm._ollama.close = AsyncMock()

    inv = InvoicingAgent(router, settings)
    log = LoggerAgent(router)

    await router.start()
    await sched.start()
    await comm.start()
    await inv.start()
    await log.start()
    await asyncio.sleep(0.1)

    yield router, db, settings

    await log.stop()
    await inv.stop()
    await comm.stop()
    await sched.stop()
    await router.stop()
    await store.close()
    await close_database(db)


def _next_business_day():
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    while tomorrow.weekday() >= 5:
        tomorrow += timedelta(days=1)
    return tomorrow.date().isoformat()


class TestE2EMultipleBookings:

    async def test_two_dogs_same_slot_same_group(self, e2e_system):
        """Two adult dogs booked at the same slot should share a group."""
        router, db, settings = e2e_system

        walk_date = _next_business_day()
        rows = await db.execute_fetchall(
            """SELECT c.email, c.name, d.name as dog_name, d.id as dog_id, d.age_months
               FROM clients c JOIN dogs d ON c.id = d.client_id
               WHERE d.age_months > 10
               ORDER BY c.name LIMIT 2"""
        )

        # Book two adult dogs at the same slot
        for r in rows:
            client = dict(r)
            await router.publish(BookingIntent(
                client_email=client["email"],
                client_name=client["name"],
                dog_name=client["dog_name"],
                walk_date=walk_date,
                walk_slot="11:30",
            ))
            await _drain(0.3)

        # Check they're in the same group
        walks = await db.execute_fetchall(
            """SELECT * FROM walks WHERE date = ? AND slot = '11:30' AND status = 'scheduled'""",
            (walk_date,),
        )
        assert len(walks) == 2
        group_ids = {dict(w)["group_id"] for w in walks}
        assert len(group_ids) == 1  # Same group

    async def test_puppy_goes_to_puppy_group(self, e2e_system):
        """A puppy (4-10 months) should be placed in a puppy group, not mixed with adults."""
        router, db, settings = e2e_system

        walk_date = _next_business_day()

        # Find a puppy
        puppy_rows = await db.execute_fetchall(
            """SELECT c.email, c.name, d.name as dog_name, d.age_months
               FROM clients c JOIN dogs d ON c.id = d.client_id
               WHERE d.age_months BETWEEN 4 AND 10 LIMIT 1"""
        )
        assert len(puppy_rows) > 0
        puppy = dict(puppy_rows[0])

        # Find an adult
        adult_rows = await db.execute_fetchall(
            """SELECT c.email, c.name, d.name as dog_name, d.age_months
               FROM clients c JOIN dogs d ON c.id = d.client_id
               WHERE d.age_months > 10 LIMIT 1"""
        )
        adult = dict(adult_rows[0])

        # Book both at the same slot
        for c in [puppy, adult]:
            await router.publish(BookingIntent(
                client_email=c["email"],
                client_name=c["name"],
                dog_name=c["dog_name"],
                walk_date=walk_date,
                walk_slot="12:00",
            ))
            await _drain(0.3)

        # They should be in different groups
        walks = await db.execute_fetchall(
            """SELECT w.*, d.age_months, g.group_type
               FROM walks w
               JOIN dogs d ON w.dog_id = d.id
               LEFT JOIN walk_groups g ON w.group_id = g.id
               WHERE w.date = ? AND w.slot = '12:00' AND w.status = 'scheduled'""",
            (walk_date,),
        )
        assert len(walks) == 2

        for w in walks:
            wd = dict(w)
            if wd["age_months"] <= 10:
                assert wd["group_type"] == "puppy"
            else:
                assert wd["group_type"] == "standard"

        # Different group IDs
        group_ids = {dict(w)["group_id"] for w in walks}
        assert len(group_ids) == 2

    async def test_three_dogs_fill_group(self, e2e_system):
        """Three dogs at the same slot should all fit in one group (max 4)."""
        router, db, settings = e2e_system

        walk_date = _next_business_day()
        rows = await db.execute_fetchall(
            """SELECT c.email, c.name, d.name as dog_name
               FROM clients c JOIN dogs d ON c.id = d.client_id
               WHERE d.age_months > 10
               ORDER BY c.name LIMIT 3"""
        )

        for r in rows:
            client = dict(r)
            await router.publish(BookingIntent(
                client_email=client["email"],
                client_name=client["name"],
                dog_name=client["dog_name"],
                walk_date=walk_date,
                walk_slot="12:30",
            ))
            await _drain(0.3)

        walks = await db.execute_fetchall(
            "SELECT * FROM walks WHERE date = ? AND slot = '12:30' AND status = 'scheduled'",
            (walk_date,),
        )
        assert len(walks) == 3
        group_ids = {dict(w)["group_id"] for w in walks}
        assert len(group_ids) == 1


class TestE2EPaymentFlow:

    async def test_mark_invoice_paid(self, e2e_system):
        """PaymentConfirmed should mark invoice as paid."""
        router, db, settings = e2e_system

        walk_date = _next_business_day()
        rows = await db.execute_fetchall(
            """SELECT c.email, c.name, d.name as dog_name
               FROM clients c JOIN dogs d ON c.id = d.client_id LIMIT 1"""
        )
        client = dict(rows[0])

        await router.publish(BookingIntent(
            client_email=client["email"],
            client_name=client["name"],
            dog_name=client["dog_name"],
            walk_date=walk_date,
            walk_slot="11:30",
        ))
        await _drain(0.5)

        # Get the invoice
        invoices = await db.execute_fetchall("SELECT * FROM invoices WHERE status = 'pending'")
        assert len(invoices) >= 1
        inv = dict(invoices[0])

        # Mark as paid
        await router.publish(PaymentConfirmed(
            invoice_id=inv["id"],
            client_email=client["email"],
            amount_eur=20.0,
        ))
        await _drain(0.5)

        # Verify
        paid_inv = await db.execute_fetchall("SELECT * FROM invoices WHERE id = ?", (inv["id"],))
        assert dict(paid_inv[0])["status"] == "paid"
        assert dict(paid_inv[0])["paid_date"] is not None


class TestE2EJournalCompleteness:

    async def test_journal_captures_full_chain(self, e2e_system):
        """Journal should capture the full event chain for a booking."""
        router, db, settings = e2e_system

        walk_date = _next_business_day()
        rows = await db.execute_fetchall(
            """SELECT c.email, c.name, d.name as dog_name
               FROM clients c JOIN dogs d ON c.id = d.client_id LIMIT 1"""
        )
        client = dict(rows[0])

        await router.publish(BookingIntent(
            client_email=client["email"],
            client_name=client["name"],
            dog_name=client["dog_name"],
            walk_date=walk_date,
            walk_slot="11:30",
        ))
        await _drain(0.5)

        journal = await db.execute_fetchall(
            "SELECT event_type, actor FROM journal ORDER BY timestamp"
        )
        event_types = [dict(j)["event_type"] for j in journal]

        # The chain: BookingIntent → ScheduleConfirmed → ConfirmationSent → InvoiceGenerated
        assert "BookingIntent" in event_types
        assert "ScheduleConfirmed" in event_types
        assert "InvoiceGenerated" in event_types

        # Verify ordering: BookingIntent before ScheduleConfirmed
        booking_idx = event_types.index("BookingIntent")
        sched_idx = event_types.index("ScheduleConfirmed")
        assert booking_idx < sched_idx
