"""End-to-end integration tests — full workflow from event to outcome.

These tests verify the complete multi-agent pipeline:
  Email → IntakeAgent → SchedulingAgent → CommunicationAgent → InvoicingAgent → LoggerAgent

All external services (Ollama LLM, IMAP, SMTP) are mocked.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.router.event import (
    BookingIntent,
    CancellationIntent,
    ClarificationRequest,
    ComplaintIntent,
    HumanApprovalRequired,
    HumanApproved,
    ScheduleConfirmed,
    CancellationConfirmed,
    InvoiceGenerated,
    PaymentConfirmed,
    ConfirmationSent,
    MessageSent,
    JournalEntry,
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


@pytest.fixture
async def e2e_system(tmp_path):
    """Full system with all agents (except Intake/Reminder which are poll-driven).

    We publish events directly to the router, simulating what IntakeAgent
    would emit after parsing an email. This tests the downstream pipeline.
    """
    db_path = str(tmp_path / "e2e.db")
    db = await init_database(db_path)
    await generate_seed_data(db)

    store = EventStore(db_path)
    await store.init()
    router = EventRouter(store)

    settings = Settings(
        db_path=db_path,
        imap_host="",
        imap_user="",
        imap_password="",
        smtp_host="",
        smtp_user="test@walking-hounds.local",
        smtp_password="",
        ollama_host="http://localhost:11434",
        ollama_model="llama3.1:8b",
    )

    # Create agents with mocked external deps
    sched = SchedulingAgent(router, settings)
    inv = InvoicingAgent(router, settings)
    log = LoggerAgent(router)

    # CommunicationAgent needs mocked SMTP and Ollama
    comm = CommunicationAgent(router, settings)
    comm._smtp = MagicMock()
    comm._smtp.send = AsyncMock(return_value=True)
    comm._ollama = MagicMock()
    comm._ollama.generate = AsyncMock(return_value="This is a test response.")
    comm._ollama.init = AsyncMock()
    comm._ollama.close = AsyncMock()

    # Start everything
    await router.start()
    await sched.start()
    await comm.start()
    await inv.start()
    await log.start()

    # Allow agents to register subscriptions
    await asyncio.sleep(0.1)

    yield router, db, settings

    # Teardown
    await log.stop()
    await inv.stop()
    await comm.stop()
    await sched.stop()
    await router.stop()
    await store.close()
    await close_database(db)


async def _drain(timeout: float = 1.0):
    """Give async agents time to process events."""
    await asyncio.sleep(timeout)


class TestE2EBookingFlow:
    """Test the full booking pipeline: BookingIntent → Schedule → Confirm → Invoice → Journal"""

    async def test_full_booking_pipeline(self, e2e_system):
        """A booking intent from a known client should result in:
        - Walk created in DB
        - ScheduleConfirmed event
        - Confirmation email sent
        - Invoice created
        - Journal entries logged
        """
        router, db, settings = e2e_system

        # Find a known client + dog from seed data
        rows = await db.execute_fetchall(
            """SELECT c.email, c.name, d.name as dog_name, d.id as dog_id
               FROM clients c JOIN dogs d ON c.id = d.client_id
               LIMIT 1"""
        )
        client = dict(rows[0])

        # Find next business day (Mon-Fri)
        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1))
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)
        walk_date = tomorrow.date().isoformat()

        # 1. Publish BookingIntent (what IntakeAgent would emit)
        await router.publish(BookingIntent(
            client_email=client["email"],
            client_name=client["name"],
            dog_name=client["dog_name"],
            walk_date=walk_date,
            walk_slot="11:30",
            raw_message="I'd like to book a walk for Bello tomorrow at 11:30",
        ))

        await _drain(0.5)

        # 2. Verify walk was created in DB
        walks = await db.execute_fetchall(
            "SELECT * FROM walks WHERE dog_id = ? AND date = ?",
            (client["dog_id"], walk_date),
        )
        assert len(walks) == 1
        walk = dict(walks[0])
        assert walk["status"] == "scheduled"
        assert walk["slot"] == "11:30"
        assert walk["price_eur"] == 20.0

        # 3. Verify invoice was created
        invoices = await db.execute_fetchall(
            "SELECT * FROM invoices WHERE walk_id = ?", (walk["id"],)
        )
        assert len(invoices) == 1
        inv = dict(invoices[0])
        assert inv["amount_eur"] == 20.0
        assert inv["status"] == "pending"

        # 4. Verify confirmation email was sent (SMTP mocked)
        assert comm_smtp_send_count(e2e_system) >= 1

        # 5. Verify journal entries were logged
        journal = await db.execute_fetchall("SELECT * FROM journal")
        assert len(journal) >= 3  # ScheduleConfirmed + ConfirmationSent + InvoiceGenerated

        # Check event types in journal
        event_types = {dict(j)["event_type"] for j in journal}
        assert "ScheduleConfirmed" in event_types
        assert "InvoiceGenerated" in event_types

    async def test_booking_assigns_walker(self, e2e_system):
        """Booking should assign an available walker."""
        router, db, settings = e2e_system

        rows = await db.execute_fetchall(
            """SELECT c.email, c.name, d.name as dog_name, d.id as dog_id
               FROM clients c JOIN dogs d ON c.id = d.client_id
               LIMIT 1"""
        )
        client = dict(rows[0])

        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1))
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)

        await router.publish(BookingIntent(
            client_email=client["email"],
            client_name=client["name"],
            dog_name=client["dog_name"],
            walk_date=tomorrow.date().isoformat(),
            walk_slot="12:00",
        ))

        await _drain(0.5)

        walks = await db.execute_fetchall(
            "SELECT * FROM walks WHERE dog_id = ? AND date = ?",
            (client["dog_id"], tomorrow.date().isoformat()),
        )
        assert len(walks) == 1
        walk = dict(walks[0])
        assert walk["walker_id"] is not None

        # Verify walker exists
        walkers = await db.execute_fetchall(
            "SELECT * FROM walkers WHERE id = ?", (walk["walker_id"],)
        )
        assert len(walkers) == 1

    async def test_booking_creates_group(self, e2e_system):
        """Booking should create or join a walk group."""
        router, db, settings = e2e_system

        rows = await db.execute_fetchall(
            """SELECT c.email, c.name, d.name as dog_name, d.id as dog_id
               FROM clients c JOIN dogs d ON c.id = d.client_id
               LIMIT 1"""
        )
        client = dict(rows[0])

        tomorrow = (datetime.now(timezone.utc) + timedelta(days=1))
        while tomorrow.weekday() >= 5:
            tomorrow += timedelta(days=1)

        await router.publish(BookingIntent(
            client_email=client["email"],
            client_name=client["name"],
            dog_name=client["dog_name"],
            walk_date=tomorrow.date().isoformat(),
            walk_slot="11:30",
        ))

        await _drain(0.5)

        walks = await db.execute_fetchall(
            "SELECT * FROM walks WHERE dog_id = ? AND date = ?",
            (client["dog_id"], tomorrow.date().isoformat()),
        )
        walk = dict(walks[0])
        assert walk["group_id"] is not None

        groups = await db.execute_fetchall(
            "SELECT * FROM walk_groups WHERE id = ?", (walk["group_id"],)
        )
        assert len(groups) == 1
        group = dict(groups[0])
        assert group["max_dogs"] == 4


def comm_smtp_send_count(e2e_system) -> int:
    """Get the number of SMTP send calls from the mocked CommunicationAgent."""
    router, db, settings = e2e_system
    # Find the CommunicationAgent's mocked SMTP
    # It's stored in the router's subscriber list
    for subs in router._subscribers.values():
        for queue, handler, name in subs:
            if "CommunicationAgent" in name:
                # The handler is bound to the agent instance
                agent = handler.__self__ if hasattr(handler, '__self__') else None
                if agent and hasattr(agent, '_smtp'):
                    return agent._smtp.send.call_count
    return 0
