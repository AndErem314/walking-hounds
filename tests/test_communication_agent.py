"""Tests for the CommunicationAgent — template rendering, SMTP mock, gates.

Uses mock SMTP and mock Ollama — no real network calls.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.communication import CommunicationAgent, _NEGATIVE_KEYWORDS
from src.router.router import EventRouter
from src.router.event import (
    CancellationConfirmed,
    ClarificationRequest,
    ComplaintIntent,
    ConfirmationSent,
    HumanApprovalRequired,
    MessageSent,
    PaymentReminder,
    QueryIntent,
    ReminderDue,
    ScheduleConfirmed,
    ScheduleUpdated,
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
    return Settings(db_path=tmp_db_path)


def _track(router):
    emitted = []
    original = router.publish

    async def tracker(event):
        emitted.append(event)
        await original(event)

    router.publish = tracker
    return emitted


def _mock_agent(router, settings):
    """Create a CommunicationAgent with mocked SMTP and Ollama."""
    agent = CommunicationAgent(router, settings)
    agent._smtp = MagicMock()
    agent._smtp.send = AsyncMock(return_value=True)
    agent._ollama = AsyncMock()
    agent._ollama.generate = AsyncMock(return_value="Mocked LLM response.")
    agent._ollama.generate_json = AsyncMock(return_value={})
    return agent


class TestBookingConfirmation:
    async def test_sends_confirmation_email(self, setup_system, settings):
        router, db = setup_system
        agent = _mock_agent(router, settings)
        await agent.start()
        emitted = _track(router)

        await router.publish(ScheduleConfirmed(
            booking_id="walk-001",
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            dog_name="Bello",
            walker_id="w1",
            walker_name="Sarah Klein",
            walk_date="2025-07-04",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        # SMTP was called
        agent._smtp.send.assert_called_once()
        args = agent._smtp.send.call_args
        assert args[0][0] == "lisa.mueller@example.com"  # to_email
        assert "confirmed" in args[0][1].lower()  # subject
        assert "Bello" in args[0][2]  # body

        # ConfirmationSent event emitted
        confirmations = [e for e in emitted if isinstance(e, ConfirmationSent)]
        assert len(confirmations) == 1

        # Message recorded in DB
        msgs = await db.execute_fetchall("SELECT * FROM messages WHERE direction='outbound'")
        assert len(msgs) == 1
        assert msgs[0]["status"] == "sent"

        await agent.stop()

    async def test_smtp_failure_still_records(self, setup_system, settings):
        router, db = setup_system
        agent = _mock_agent(router, settings)
        agent._smtp.send = AsyncMock(return_value=False)  # SMTP fails
        await agent.start()
        emitted = _track(router)

        await router.publish(ScheduleConfirmed(
            booking_id="walk-002",
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            dog_name="Bello",
            walker_id="w1",
            walker_name="Sarah Klein",
            walk_date="2025-07-04",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        # Message recorded as failed
        msgs = await db.execute_fetchall("SELECT * FROM messages WHERE status='failed'")
        assert len(msgs) == 1

        # No ConfirmationSent emitted
        confirmations = [e for e in emitted if isinstance(e, ConfirmationSent)]
        assert len(confirmations) == 0

        await agent.stop()


class TestCancellationConfirmation:
    async def test_sends_cancellation_email(self, setup_system, settings):
        router, db = setup_system
        agent = _mock_agent(router, settings)
        await agent.start()
        emitted = _track(router)

        await router.publish(CancellationConfirmed(
            booking_id="walk-001",
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            refund_percent=50,
            late_cancellation=True,
        ))
        await asyncio.sleep(0.3)

        agent._smtp.send.assert_called_once()
        args = agent._smtp.send.call_args
        assert "cancelled" in args[0][1].lower()
        assert "50%" in args[0][2]

        messages = [e for e in emitted if isinstance(e, MessageSent)]
        assert len(messages) == 1
        assert messages[0].message_type == "cancellation_confirmation"

        await agent.stop()


class TestClarificationRequest:
    async def test_sends_clarification_email(self, setup_system, settings):
        router, db = setup_system
        agent = _mock_agent(router, settings)
        await agent.start()
        emitted = _track(router)

        await router.publish(ClarificationRequest(
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            intent="booking",
            missing_fields=["dog_name", "walk_date"],
            suggested_clarification="your dog's name, the specific date",
        ))
        await asyncio.sleep(0.3)

        agent._smtp.send.assert_called_once()
        args = agent._smtp.send.call_args
        assert "more info" in args[0][1].lower()
        assert "your dog's name" in args[0][2]

        messages = [e for e in emitted if isinstance(e, MessageSent)]
        assert len(messages) == 1
        assert messages[0].message_type == "clarification_request"

        await agent.stop()


class TestQueryResponse:
    async def test_sends_llm_drafted_query_response(self, setup_system, settings):
        router, db = setup_system
        agent = _mock_agent(router, settings)
        await agent.start()
        emitted = _track(router)

        await router.publish(QueryIntent(
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            query_text="How much does a walk cost?",
        ))
        await asyncio.sleep(0.3)

        # LLM was called
        agent._ollama.generate.assert_called_once()

        # Email was sent
        agent._smtp.send.assert_called_once()
        args = agent._smtp.send.call_args
        assert "Mocked LLM response" in args[0][2]

        messages = [e for e in emitted if isinstance(e, MessageSent)]
        assert len(messages) == 1
        assert messages[0].message_type == "query_response"

        await agent.stop()


class TestComplaintGate:
    async def test_complaint_always_routes_to_human_gate(self, setup_system, settings):
        router, db = setup_system
        agent = _mock_agent(router, settings)
        await agent.start()
        emitted = _track(router)

        await router.publish(ComplaintIntent(
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            complaint_text="The walker was 30 minutes late and didn't clean up!",
            severity="high",
        ))
        await asyncio.sleep(0.3)

        # LLM was called to draft a response
        agent._ollama.generate.assert_called_once()

        # HumanApprovalRequired emitted (NOT sent directly)
        gates = [e for e in emitted if isinstance(e, HumanApprovalRequired)]
        assert len(gates) == 1
        assert gates[0].gate_type == "complaint_response"
        assert "drafted_response" in gates[0].context
        assert "Mocked LLM response" in gates[0].context["drafted_response"]

        # No email was sent directly
        agent._smtp.send.assert_not_called()

        await agent.stop()


class TestPaymentReminder:
    async def test_sends_payment_reminder(self, setup_system, settings):
        router, db = setup_system
        agent = _mock_agent(router, settings)
        await agent.start()
        emitted = _track(router)

        await router.publish(PaymentReminder(
            invoice_id="inv-001",
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            reminder_count=1,
            amount_eur=20.0,
        ))
        await asyncio.sleep(0.3)

        agent._smtp.send.assert_called_once()
        args = agent._smtp.send.call_args
        assert "gentle reminder" in args[0][2]

        messages = [e for e in emitted if isinstance(e, MessageSent)]
        assert len(messages) == 1
        assert messages[0].message_type == "payment_reminder"

        await agent.stop()


class TestNegativeSentimentDetection:
    def test_detects_negative_keywords(self):
        assert CommunicationAgent._detect_negative_sentiment("The walker was late!")
        assert CommunicationAgent._detect_negative_sentiment("This is unacceptable")
        assert CommunicationAgent._detect_negative_sentiment("I want a refund")

    def test_positive_text_not_flagged(self):
        assert not CommunicationAgent._detect_negative_sentiment("Thank you for the great walk!")
        assert not CommunicationAgent._detect_negative_sentiment("Bello loved it!")

    def test_all_negative_keywords_listed(self):
        assert len(_NEGATIVE_KEYWORDS) >= 15
