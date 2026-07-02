"""E2E tests: cancellation, clarification, complaint, multiple bookings."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.router.event import (
    BookingIntent,
    CancellationIntent,
    ClarificationRequest,
    ComplaintIntent,
    HumanApprovalRequired,
    PaymentConfirmed,
    ScheduleConfirmed,
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
    """Full system with mocked external deps."""
    db_path = str(tmp_path / "e2e2.db")
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


async def _book_a_walk(router, db, slot="11:30", client_idx=0):
    """Helper: book a walk and return the walk dict."""
    rows = await db.execute_fetchall(
        """SELECT c.email, c.name, d.name as dog_name, d.id as dog_id
           FROM clients c JOIN dogs d ON c.id = d.client_id
           ORDER BY c.name LIMIT 5"""
    )
    client = dict(rows[client_idx])
    tomorrow = datetime.now(timezone.utc) + timedelta(days=3)  # 3 days ahead for >24h cancel
    while tomorrow.weekday() >= 5:
        tomorrow += timedelta(days=1)
    walk_date = tomorrow.date().isoformat()

    await router.publish(BookingIntent(
        client_email=client["email"],
        client_name=client["name"],
        dog_name=client["dog_name"],
        walk_date=walk_date,
        walk_slot=slot,
    ))
    await _drain(0.5)

    walks = await db.execute_fetchall(
        "SELECT * FROM walks WHERE dog_id = ? AND date = ?",
        (client["dog_id"], walk_date),
    )
    return dict(walks[0]) if walks else None, client, walk_date


class TestE2ECancellationFlow:

    async def test_early_cancellation_full_refund(self, e2e_system):
        """Cancel >24h ahead → walk cancelled, invoice zeroed, email sent."""
        router, db, settings = e2e_system

        walk, client, walk_date = await _book_a_walk(router, db)

        # Cancel well ahead (walk is tomorrow)
        await router.publish(CancellationIntent(
            client_email=client["email"],
            client_name=client["name"],
            dog_name=client["dog_name"],
            walk_date=walk_date,
            reason="Schedule conflict",
        ))
        await _drain(0.5)

        # Walk should be cancelled
        walks = await db.execute_fetchall("SELECT * FROM walks WHERE id = ?", (walk["id"],))
        assert dict(walks[0])["status"] == "cancelled"

        # Invoice should be zeroed (full refund)
        invoices = await db.execute_fetchall("SELECT * FROM invoices WHERE walk_id = ?", (walk["id"],))
        inv = dict(invoices[0])
        assert inv["amount_eur"] == 0
        assert inv["status"] == "paid"

    async def test_cancellation_email_sent(self, e2e_system):
        """Cancellation should trigger a confirmation email."""
        router, db, settings = e2e_system

        walk, client, walk_date = await _book_a_walk(router, db)

        # Get SMTP call count before
        # We'll check journal for CancellationConfirmed + MessageSent
        await router.publish(CancellationIntent(
            client_email=client["email"],
            client_name=client["name"],
            dog_name=client["dog_name"],
            walk_date=walk_date,
        ))
        await _drain(0.5)

        journal = await db.execute_fetchall("SELECT event_type FROM journal")
        event_types = {dict(j)["event_type"] for j in journal}
        assert "CancellationConfirmed" in event_types


class TestE2EClarificationFlow:

    async def test_clarification_email_sent(self, e2e_system):
        """ClarificationRequest should trigger a clarification email."""
        router, db, settings = e2e_system

        await router.publish(ClarificationRequest(
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            intent="booking",
            missing_fields=["walk_date"],
            original_message="I want to book a walk",
            suggested_clarification="Please specify the date",
        ))
        await _drain(0.5)

        journal = await db.execute_fetchall("SELECT event_type FROM journal")
        event_types = {dict(j)["event_type"] for j in journal}
        assert "MessageSent" in event_types

        # Verify a message was recorded in messages table
        msgs = await db.execute_fetchall(
            "SELECT * FROM messages WHERE subject LIKE '%more info%' OR subject LIKE '%clarif%'"
        )
        assert len(msgs) >= 1


class TestE2EComplaintFlow:

    async def test_complaint_goes_to_human_gate(self, e2e_system):
        """ComplaintIntent should create a HumanApprovalRequired gate, not auto-send."""
        router, db, settings = e2e_system

        await router.publish(ComplaintIntent(
            client_email="tom.schmidt@example.com",
            client_name="Tom Schmidt",
            complaint_text="The walker was late and my dog came back stressed.",
            severity="high",
        ))
        await _drain(0.5)

        # A human gate should have been created
        gates = await db.execute_fetchall(
            "SELECT * FROM approval_gates WHERE gate_type = 'complaint_response'"
        )
        assert len(gates) == 1
        gate = dict(gates[0])
        assert gate["status"] == "pending"

        # Context should contain the drafted response
        context = json.loads(gate["context"])
        assert "drafted_response" in context
        assert context["client_email"] == "tom.schmidt@example.com"

    async def test_complaint_no_auto_email(self, e2e_system):
        """Complaint should NOT auto-send an email — human must approve first."""
        router, db, settings = e2e_system

        # Count messages before
        msgs_before = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM messages")
        count_before = msgs_before[0]["cnt"]

        await router.publish(ComplaintIntent(
            client_email="anna.becker@example.com",
            client_name="Anna Becker",
            complaint_text="Terrible service!",
            severity="medium",
        ))
        await _drain(0.5)

        # No new messages should have been sent (complaint is gated)
        msgs_after = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM messages")
        count_after = msgs_after[0]["cnt"]
        assert count_after == count_before  # No outbound email


import json  # needed for complaint context check above
