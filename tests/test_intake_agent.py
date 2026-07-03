"""Tests for the IntakeAgent — LLM parsing, clarity check, dedup, client lookup, gating.

Uses a mock OllamaClient — no real network calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.intake import IntakeAgent, SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from src.router.router import EventRouter
from src.router.event import (
    BookingIntent,
    CancellationIntent,
    ClarificationRequest,
    ComplaintIntent,
    DogInfoProvided,
    HumanApprovalRequired,
    OnboardingStarted,
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
    return Settings(db_path=tmp_db_path, imap_poll_interval_sec=999, intake_demo_mode=False)


def _track_publish(router):
    """Wrap router.publish to capture emitted events."""
    emitted = []
    original = router.publish

    async def tracker(event):
        emitted.append(event)
        await original(event)

    router.publish = tracker
    return emitted


class TestIntakeAgentParsing:
    """Test that the LLM JSON response is correctly mapped to events."""

    async def test_booking_email_emits_booking_intent(self, setup_system, settings):
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "booking",
            "clarity": "clear",
            "missing_fields": [],
            "client_name": "Lisa Müller",
            "dog_name": "Bello",
            "walk_date": "2025-07-04",
            "walk_slot": "11:30",
            "reason": None,
            "severity": None,
            "summary": "Lisa wants to book a walk for Bello",
        })

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "test-booking-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Walk for Bello",
            "body": "Hi, can you walk Bello on Friday at 11:30?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        })

        bookings = [e for e in emitted if isinstance(e, BookingIntent)]
        assert len(bookings) == 1
        assert bookings[0].dog_name == "Bello"
        assert bookings[0].walk_date == "2025-07-04"
        assert bookings[0].walk_slot == "11:30"

    async def test_cancellation_email_emits_cancellation_intent(self, setup_system, settings):
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "cancellation",
            "clarity": "clear",
            "missing_fields": [],
            "client_name": "Lisa Müller",
            "dog_name": "Bello",
            "walk_date": "2025-07-04",
            "walk_slot": None,
            "reason": "Going on vacation",
            "severity": None,
            "summary": "Lisa cancels Friday's walk",
        })

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "test-cancel-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Cancel walk",
            "body": "Sorry, need to cancel Bello's walk on Friday. Going on vacation.",
            "date": "Thu, 03 Jul 2025 08:00:00 +0200",
        })

        cancels = [e for e in emitted if isinstance(e, CancellationIntent)]
        assert len(cancels) == 1
        assert "vacation" in cancels[0].reason

    async def test_query_email_emits_query_intent(self, setup_system, settings):
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "query",
            "clarity": "clear",
            "missing_fields": [],
            "client_name": "Lisa Müller",
            "dog_name": None,
            "walk_date": None,
            "walk_slot": None,
            "reason": None,
            "severity": None,
            "summary": "Lisa asks about pricing",
        })

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "test-query-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Pricing question",
            "body": "How much does a walk cost?",
            "date": "Thu, 03 Jul 2025 09:00:00 +0200",
        })

        queries = [e for e in emitted if isinstance(e, QueryIntent)]
        assert len(queries) == 1

    async def test_complaint_email_emits_complaint_intent(self, setup_system, settings):
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "complaint",
            "clarity": "clear",
            "missing_fields": [],
            "client_name": "Lisa Müller",
            "dog_name": "Bello",
            "walk_date": None,
            "walk_slot": None,
            "reason": "Walker was late",
            "severity": "high",
            "summary": "Lisa complains about late walker",
        })

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "test-complaint-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Complaint about today",
            "body": "The walker was 30 minutes late today! This is unacceptable.",
            "date": "Thu, 03 Jul 2025 15:00:00 +0200",
        })

        complaints = [e for e in emitted if isinstance(e, ComplaintIntent)]
        assert len(complaints) == 1
        assert complaints[0].severity == "high"


class TestIntakeAgentClarity:
    """Test that unclear emails trigger clarification requests."""

    async def test_vague_email_triggers_clarification(self, setup_system, settings):
        """'Maybe tomorrow' → no dog name, vague date → clarification."""
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "booking",
            "clarity": "needs_clarification",
            "missing_fields": ["dog_name", "walk_date"],
            "client_name": "Lisa Müller",
            "dog_name": None,
            "walk_date": None,
            "walk_slot": None,
            "reason": None,
            "severity": None,
            "summary": "Vague booking request",
        })

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "vague-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Maybe tomorrow?",
            "body": "Maybe tomorrow?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        })

        clarifications = [e for e in emitted if isinstance(e, ClarificationRequest)]
        assert len(clarifications) == 1
        assert "dog_name" in clarifications[0].missing_fields
        assert "walk_date" in clarifications[0].missing_fields
        assert "your dog's name" in clarifications[0].suggested_clarification

    async def test_missing_dog_name_triggers_clarification(self, setup_system, settings):
        """'Walk my dog Friday 11:30' → has date, has slot, but no dog name."""
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "booking",
            "clarity": "needs_clarification",
            "missing_fields": ["dog_name"],
            "client_name": "Lisa Müller",
            "dog_name": None,
            "walk_date": "2025-07-04",
            "walk_slot": "11:30",
            "reason": None,
            "severity": None,
            "summary": "Booking without dog name",
        })

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "no-dog-name-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Walk my dog",
            "body": "Can you walk my dog this Friday at 11:30?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        })

        clarifications = [e for e in emitted if isinstance(e, ClarificationRequest)]
        assert len(clarifications) == 1
        assert "dog_name" in clarifications[0].missing_fields

    async def test_clear_booking_does_not_clarify(self, setup_system, settings):
        """'Walk Bello Friday 11:30' → has everything → no clarification."""
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "booking",
            "clarity": "clear",
            "missing_fields": [],
            "client_name": "Lisa Müller",
            "dog_name": "Bello",
            "walk_date": "2025-07-04",
            "walk_slot": "11:30",
            "reason": None,
            "severity": None,
            "summary": "Clear booking",
        })

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "clear-booking-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Walk Bello",
            "body": "Walk Bello this Friday at 11:30",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        })

        clarifications = [e for e in emitted if isinstance(e, ClarificationRequest)]
        assert len(clarifications) == 0
        bookings = [e for e in emitted if isinstance(e, BookingIntent)]
        assert len(bookings) == 1

    async def test_any_available_slot_not_treated_as_missing(self, setup_system, settings):
        """'Walk Luna next Monday any time' → LLM flags walk_slot as missing,
        but safety net overrides clarity to 'clear' since walk_slot is optional."""
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        # Simulate LLM incorrectly flagging walk_slot as missing
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "booking",
            "clarity": "needs_clarification",
            "missing_fields": ["walk_slot"],  # LLM error — walk_slot is optional
            "client_name": "Tom Schmidt",
            "dog_name": "Luna",
            "walk_date": "next Monday",  # LLM returns relative, _resolve_date handles it
            "walk_slot": None,
            "reason": None,
            "severity": None,
            "summary": "Tom wants to book Luna for next Monday, any time",
        })

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "any-slot-001",
            "from_email": "tom.schmidt@example.com",
            "subject": "Walk Luna next Monday",
            "body": "I would like to book a walk for Luna for next Monday, for any available time slot. Tom Schmidt.",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        })

        # Should NOT emit clarification — walk_slot is optional for bookings
        clarifications = [e for e in emitted if isinstance(e, ClarificationRequest)]
        assert len(clarifications) == 0, (
            f"Expected no clarification, got: {[c.missing_fields for c in clarifications]}"
        )
        # Should emit a BookingIntent with walk_slot=None — system auto-assigns
        bookings = [e for e in emitted if isinstance(e, BookingIntent)]
        assert len(bookings) == 1
        assert bookings[0].dog_name == "Luna"
        assert bookings[0].walk_slot is None  # system will auto-assign


class TestIntakeAgentDedup:
    """Test that duplicate emails are not processed twice."""

    async def test_duplicate_email_skipped(self, setup_system, settings):
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "booking",
            "clarity": "clear",
            "missing_fields": [],
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

        await agent._process_email(email_data)
        await agent._process_email(email_data)

        rows = await db.execute_fetchall(
            "SELECT COUNT(*) as cnt FROM processed_emails WHERE message_id = ?",
            ("dedup-001",),
        )
        assert rows[0]["cnt"] == 1


class TestIntakeAgentNewClientGate:
    """Test that unknown clients trigger the OnboardingStarted event."""

    async def test_unknown_client_triggers_onboarding_started(self, setup_system, settings):
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "booking",
            "clarity": "clear",
            "missing_fields": [],
            "client_name": "New Person",
            "dog_name": "NewDog",
            "walk_date": "2025-07-04",
            "walk_slot": "12:00",
            "reason": None,
            "severity": None,
            "summary": "New client booking",
        })

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "new-client-001",
            "from_email": "newperson@example.com",
            "subject": "New booking",
            "body": "Hi, I'm new. Can you walk my dog NewDog on Friday at 12:00?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        })

        onboardings = [e for e in emitted if isinstance(e, OnboardingStarted)]
        assert len(onboardings) == 1
        assert onboardings[0].client_email == "newperson@example.com"
        assert onboardings[0].original_intent == "booking"

    async def test_unknown_client_query_does_not_gate(self, setup_system, settings):
        """Queries from unknown senders should pass through."""
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "query",
            "clarity": "clear",
            "missing_fields": [],
            "client_name": "Stranger",
            "dog_name": None,
            "walk_date": None,
            "walk_slot": None,
            "reason": None,
            "severity": None,
            "summary": "Price inquiry",
        })

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "unknown-query-001",
            "from_email": "stranger@example.com",
            "subject": "Question",
            "body": "How much do walks cost?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        })

        queries = [e for e in emitted if isinstance(e, QueryIntent)]
        assert len(queries) == 1
        gates = [e for e in emitted if isinstance(e, HumanApprovalRequired)]
        assert len(gates) == 0


class TestIntakeAgentOnboardingDetection:
    """Test that onboarding replies are detected and emit DogInfoProvided."""

    async def test_onboarding_reply_emits_dog_info(self, setup_system, settings):
        router, db = setup_system
        agent = IntakeAgent(router, settings)

        # Create an active onboarding session for this email
        import json
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO onboarding_sessions (id, email, status, first_contact_at, last_contact_at)
               VALUES (?, ?, 'awaiting_info', ?, ?)""",
            ("sess-001", "newclient@example.com", now, now),
        )
        await db.commit()

        # Mock LLM: first call = standard parse, second call = onboarding parse
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(side_effect=[
            {  # Standard parse — booking intent (triggers onboarding check)
                "intent": "booking",
                "clarity": "clear",
                "missing_fields": [],
                "client_name": "New Client",
                "dog_name": "Rex",
                "walk_date": "2025-07-10",
                "walk_slot": "12:00",
                "reason": None,
                "severity": None,
                "summary": "Booking",
            },
            {  # Onboarding parse — extracts dog info
                "client_name": "New Client",
                "dog_name": "Rex",
                "breed": "German Shepherd",
                "age_months": 36,
                "sex": "male",
                "castrated": "neutered",
                "temperament": "energetic",
                "special_needs": "",
                "summary": "Dog info",
            },
        ])

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "onboard-reply-001",
            "from_email": "newclient@example.com",
            "subject": "Re: Welcome — dog info",
            "body": "My dog is Rex, a 3-year-old German Shepherd, male, neutered, very energetic.",
            "date": "Fri, 11 Jul 2025 10:00:00 +0200",
        })

        # Should emit DogInfoProvided, not BookingIntent or OnboardingStarted
        dog_infos = [e for e in emitted if isinstance(e, DogInfoProvided)]
        onboardings = [e for e in emitted if isinstance(e, OnboardingStarted)]
        bookings = [e for e in emitted if isinstance(e, BookingIntent)]

        assert len(dog_infos) == 1
        assert len(onboardings) == 0
        assert len(bookings) == 0
        assert dog_infos[0].dog_name == "Rex"
        assert dog_infos[0].breed == "German Shepherd"
        assert dog_infos[0].sex == "male"
        assert dog_infos[0].age_months == 36


class TestIntakeAgentReschedule:
    """Test reschedule intent."""

    async def test_reschedule_emits_reschedule_intent(self, setup_system, settings):
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "reschedule",
            "clarity": "clear",
            "missing_fields": [],
            "client_name": "Lisa Müller",
            "dog_name": "Bello",
            "walk_date": "2025-07-05",
            "walk_slot": "12:00",
            "reason": None,
            "severity": None,
            "summary": "Reschedule to Saturday",
        })

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "reschedule-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Move walk",
            "body": "Can we move Bello's walk to Saturday at 12:00?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        })

        reschedules = [e for e in emitted if isinstance(e, RescheduleIntent)]
        assert len(reschedules) == 1
        assert reschedules[0].new_date == "2025-07-05"
        assert reschedules[0].new_slot == "12:00"


class TestIntakeAgentOtherIntent:
    """Test that 'other' intent triggers a human gate."""

    async def test_other_intent_triggers_unusual_request_gate(self, setup_system, settings):
        router, db = setup_system
        agent = IntakeAgent(router, settings)
        agent._ollama = AsyncMock()
        agent._ollama.generate_json = AsyncMock(return_value={
            "intent": "other",
            "clarity": "clear",
            "missing_fields": [],
            "client_name": "Lisa Müller",
            "dog_name": None,
            "walk_date": None,
            "walk_slot": None,
            "reason": None,
            "severity": None,
            "summary": "Something unusual",
        })

        emitted = _track_publish(router)

        await agent._process_email({
            "message_id": "other-001",
            "from_email": "lisa.mueller@example.com",
            "subject": "Strange request",
            "body": "Can you pet-sit my cat next week?",
            "date": "Thu, 03 Jul 2025 10:00:00 +0200",
        })

        gates = [e for e in emitted if isinstance(e, HumanApprovalRequired)]
        assert len(gates) == 1
        assert gates[0].gate_type == "unusual_request"


class TestClarificationText:
    """Test the _build_clarification_text static method."""

    def test_missing_dog_name(self):
        text = IntakeAgent._build_clarification_text("booking", ["dog_name"])
        assert "your dog's name" in text

    def test_missing_date(self):
        text = IntakeAgent._build_clarification_text("booking", ["walk_date"])
        assert "specific date" in text

    def test_multiple_missing_fields(self):
        text = IntakeAgent._build_clarification_text("booking", ["dog_name", "walk_date"])
        assert "your dog's name" in text
        assert "specific date" in text

    def test_intent_in_text(self):
        text = IntakeAgent._build_clarification_text("cancellation", ["dog_name"])
        assert "cancellation" in text
