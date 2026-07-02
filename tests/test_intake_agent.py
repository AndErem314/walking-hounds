"""Tests for the IntakeAgent — LLM parsing, dedup, client lookup, gating.

Uses a mock OllamaClient and mock IMAP — no real network calls.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.intake import IntakeAgent, SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from src.agents.base import BaseAgent
from src.router.router import EventRouter
from src.router.event import (
    BookingIntent,
    CancellationIntent,
    ComplaintIntent,
    HumanApprovalRequired,
    QueryIntent,
    RescheduleIntent,
)
from src.router.store import EventStore
from src.db.database import init_database, close_database
from src.db.seed import generate_seed_data
from src.config import Settings


@pytest.fixture
async def setup_system(tmp_db_path):
    """Set up router + store + database with seed data."""
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
    return Settings(db_path=tmp_db_path, imap_poll_interval_sec=999)


class TestIntakeAgentParsing:
    """Test that the LLM JSON response is correctly mapped to events."""

    async def test_booking_email_emits_booking_intent(self, setup_system, settings):
        router, db = setup_system

        agent = IntakeAgent(router, settings)
        # Mock Ollama
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "booking",
            "confidence": 0.95,
            "client_name": "Lisa Müller",
            "dog_name": "Bello",
            "walk_date": "2025-07-04",
            "walk_slot": "11:30",
            "reason": None,
            "severity": None,
            "summary": "Lisa wants to book a walk for Bello",
        })

        # Track emitted events
        emitted = []
        original_publish = router.publish

        async def track_publish(event):
            emitted.append(event)
            await original_publish(event)

        router.publish = track_publish

        email_data = {
            "message_id": "test-booking-001",
            "from_email": "lisa.mueller@example.com",  # seeded client
            "subject": "Walk for Bello",
            "body": "Hi, can you walk Bello on Friday at 11:30?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        }

        await agent._process_email(email_data)

        # Should emit a BookingIntent
        booking_events = [e for e in emitted if isinstance(e, BookingIntent)]
        assert len(booking_events) == 1
        assert booking_events[0].dog_name == "Bello"
        assert booking_events[0].walk_date == "2025-07-04"
        assert booking_events[0].walk_slot == "11:30"
        assert booking_events[0].confidence == 0.95

    async def test_cancellation_email_emits_cancellation_intent(self, setup_system, settings):
        router, db = setup_system

        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "cancellation",
            "confidence": 0.9,
            "client_name": "Lisa Müller",
            "dog_name": "Bello",
            "walk_date": "2025-07-04",
            "walk_slot": None,
            "reason": "Going on vacation",
            "severity": None,
            "summary": "Lisa cancels Friday's walk",
        })

        emitted = []
        original_publish = router.publish

        async def track_publish(event):
            emitted.append(event)
            await original_publish(event)

        router.publish = track_publish

        email_data = {
            "message_id": "test-cancel-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Cancel walk",
            "body": "Sorry, need to cancel Bello's walk on Friday. Going on vacation.",
            "date": "Thu, 03 Jul 2025 08:00:00 +0200",
        }

        await agent._process_email(email_data)

        cancel_events = [e for e in emitted if isinstance(e, CancellationIntent)]
        assert len(cancel_events) == 1
        assert "vacation" in cancel_events[0].reason

    async def test_query_email_emits_query_intent(self, setup_system, settings):
        router, db = setup_system

        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "query",
            "confidence": 0.88,
            "client_name": "Lisa Müller",
            "dog_name": None,
            "walk_date": None,
            "walk_slot": None,
            "reason": None,
            "severity": None,
            "summary": "Lisa asks about pricing",
        })

        emitted = []
        original_publish = router.publish

        async def track_publish(event):
            emitted.append(event)
            await original_publish(event)

        router.publish = track_publish

        email_data = {
            "message_id": "test-query-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Pricing question",
            "body": "How much does a walk cost?",
            "date": "Thu, 03 Jul 2025 09:00:00 +0200",
        }

        await agent._process_email(email_data)

        query_events = [e for e in emitted if isinstance(e, QueryIntent)]
        assert len(query_events) == 1

    async def test_complaint_email_emits_complaint_intent(self, setup_system, settings):
        router, db = setup_system

        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "complaint",
            "confidence": 0.92,
            "client_name": "Lisa Müller",
            "dog_name": "Bello",
            "walk_date": None,
            "walk_slot": None,
            "reason": "Walker was late",
            "severity": "high",
            "summary": "Lisa complains about late walker",
        })

        emitted = []
        original_publish = router.publish

        async def track_publish(event):
            emitted.append(event)
            await original_publish(event)

        router.publish = track_publish

        email_data = {
            "message_id": "test-complaint-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Complaint about today",
            "body": "The walker was 30 minutes late today! This is unacceptable.",
            "date": "Thu, 03 Jul 2025 15:00:00 +0200",
        }

        await agent._process_email(email_data)

        complaint_events = [e for e in emitted if isinstance(e, ComplaintIntent)]
        assert len(complaint_events) == 1
        assert complaint_events[0].severity == "high"


class TestIntakeAgentDedup:
    """Test that duplicate emails are not processed twice."""

    async def test_duplicate_email_skipped(self, setup_system, settings):
        router, db = setup_system

        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "booking",
            "confidence": 0.9,
            "client_name": "Lisa Müller",
            "dog_name": "Bello",
            "walk_date": "2025-07-04",
            "walk_slot": "11:30",
            "reason": None,
            "severity": None,
            "summary": "Booking",
        })

        email_data = {
            "message_id": "dedup-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Walk",
            "body": "Walk Bello Friday",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        }

        # Process once
        await agent._process_email(email_data)
        # Process again — should be skipped
        await agent._process_email(email_data)

        # Check only one processed_emails entry
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) as cnt FROM processed_emails WHERE message_id = ?",
            ("dedup-001",),
        )
        assert rows[0]["cnt"] == 1


class TestIntakeAgentConfidenceGate:
    """Test that low-confidence parses route to human gate."""

    async def test_low_confidence_triggers_human_gate(self, setup_system, settings):
        router, db = setup_system

        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "booking",
            "confidence": 0.40,  # Below default threshold of 0.75
            "client_name": "Unknown",
            "dog_name": None,
            "walk_date": None,
            "walk_slot": None,
            "reason": None,
            "severity": None,
            "summary": "Unclear email",
        })

        emitted = []
        original_publish = router.publish

        async def track_publish(event):
            emitted.append(event)
            await original_publish(event)

        router.publish = track_publish

        email_data = {
            "message_id": "low-conf-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Hmm",
            "body": "Maybe tomorrow?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        }

        await agent._process_email(email_data)

        gate_events = [e for e in emitted if isinstance(e, HumanApprovalRequired)]
        assert len(gate_events) == 1
        assert gate_events[0].gate_type == "ambiguous_intent"
        assert "booking" in gate_events[0].options


class TestIntakeAgentNewClientGate:
    """Test that unknown clients trigger the new_client gate."""

    async def test_unknown_client_triggers_new_client_gate(self, setup_system, settings):
        router, db = setup_system

        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "booking",
            "confidence": 0.95,
            "client_name": "New Person",
            "dog_name": "NewDog",
            "walk_date": "2025-07-04",
            "walk_slot": "12:00",
            "reason": None,
            "severity": None,
            "summary": "New client booking",
        })

        emitted = []
        original_publish = router.publish

        async def track_publish(event):
            emitted.append(event)
            await original_publish(event)

        router.publish = track_publish

        email_data = {
            "message_id": "new-client-001",
            "from_email": "newperson@example.com",  # NOT in seed data
            "subject": "New booking",
            "body": "Hi, I'm new. Can you walk my dog NewDog on Friday at 12:00?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        }

        await agent._process_email(email_data)

        gate_events = [e for e in emitted if isinstance(e, HumanApprovalRequired)]
        assert len(gate_events) == 1
        assert gate_events[0].gate_type == "new_client"
        assert "approve_and_book" in gate_events[0].options

    async def test_unknown_client_query_does_not_gate(self, setup_system, settings):
        """Queries from unknown senders should pass through (no new_client gate)."""
        router, db = setup_system

        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "query",
            "confidence": 0.9,
            "client_name": "Stranger",
            "dog_name": None,
            "walk_date": None,
            "walk_slot": None,
            "reason": None,
            "severity": None,
            "summary": "Price inquiry",
        })

        emitted = []
        original_publish = router.publish

        async def track_publish(event):
            emitted.append(event)
            await original_publish(event)

        router.publish = track_publish

        email_data = {
            "message_id": "unknown-query-001",
            "from_email": "stranger@example.com",
            "subject": "Question",
            "body": "How much do walks cost?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        }

        await agent._process_email(email_data)

        # Should emit QueryIntent, not HumanApprovalRequired
        query_events = [e for e in emitted if isinstance(e, QueryIntent)]
        assert len(query_events) == 1
        gate_events = [e for e in emitted if isinstance(e, HumanApprovalRequired)]
        assert len(gate_events) == 0


class TestIntakeAgentReschedule:
    """Test reschedule intent."""

    async def test_reschedule_emits_reschedule_intent(self, setup_system, settings):
        router, db = setup_system

        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "reschedule",
            "confidence": 0.88,
            "client_name": "Lisa Müller",
            "dog_name": "Bello",
            "walk_date": "2025-07-05",
            "walk_slot": "12:00",
            "reason": None,
            "severity": None,
            "summary": "Reschedule to Saturday",
        })

        emitted = []
        original_publish = router.publish

        async def track_publish(event):
            emitted.append(event)
            await original_publish(event)

        router.publish = track_publish

        email_data = {
            "message_id": "reschedule-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Move walk",
            "body": "Can we move Bello's walk to Saturday at 12:00?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        }

        await agent._process_email(email_data)

        reschedule_events = [e for e in emitted if isinstance(e, RescheduleIntent)]
        assert len(reschedule_events) == 1
        assert reschedule_events[0].new_date == "2025-07-05"
        assert reschedule_events[0].new_slot == "12:00"


class TestIntakeAgentOtherIntent:
    """Test that 'other' intent triggers a human gate."""

    async def test_other_intent_triggers_unusual_request_gate(self, setup_system, settings):
        router, db = setup_system

        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "other",
            "confidence": 0.85,
            "client_name": "Lisa Müller",
            "dog_name": None,
            "walk_date": None,
            "walk_slot": None,
            "reason": None,
            "severity": None,
            "summary": "Something unusual",
        })

        emitted = []
        original_publish = router.publish

        async def track_publish(event):
            emitted.append(event)
            await original_publish(event)

        router.publish = track_publish

        email_data = {
            "message_id": "other-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Strange request",
            "body": "Can you pet-sit my cat next week?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        }

        await agent._process_email(email_data)

        gate_events = [e for e in emitted if isinstance(e, HumanApprovalRequired)]
        assert len(gate_events) == 1
        assert gate_events[0].gate_type == "unusual_request"
