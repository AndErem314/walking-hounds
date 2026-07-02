"""Tests for event type definitions and serialization."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.router.event import (
    BaseEvent,
    BookingIntent,
    CancellationIntent,
    CancellationConfirmed,
    ClarificationRequest,
    ComplaintIntent,
    ConfirmationSent,
    EmailReceived,
    EventType,
    EVENT_REGISTRY,
    HumanApprovalRequired,
    HumanApproved,
    HumanRejected,
    InvoiceGenerated,
    JournalEntry,
    MessageSent,
    PaymentConfirmed,
    PaymentReminder,
    QueryIntent,
    ReminderDue,
    RescheduleIntent,
    ScheduleConfirmed,
    ScheduleConflict,
    ScheduleUpdated,
    WalkCompleted,
    deserialize_event,
    event_type_name,
)


class TestBaseEvent:
    def test_event_has_id(self):
        ev = BookingIntent(client_email="a@b.com")
        assert ev.id is not None
        assert len(ev.id) == 32  # uuid4 hex

    def test_event_has_timestamp(self):
        ev = BookingIntent(client_email="a@b.com")
        assert ev.created_at is not None
        assert ev.created_at.tzinfo is not None

    def test_event_is_frozen(self):
        ev = BookingIntent(client_email="a@b.com")
        with pytest.raises(Exception):
            ev.client_email = "changed@b.com"

    def test_correlation_id_optional(self):
        ev = BookingIntent(client_email="a@b.com", correlation_id="trace-123")
        assert ev.correlation_id == "trace-123"

        ev2 = BookingIntent(client_email="a@b.com")
        assert ev2.correlation_id is None

    def test_unique_ids(self):
        ev1 = BookingIntent(client_email="a@b.com")
        ev2 = BookingIntent(client_email="a@b.com")
        assert ev1.id != ev2.id


class TestEventTypeNames:
    def test_event_type_name_booking(self):
        ev = BookingIntent(client_email="a@b.com")
        assert event_type_name(ev) == "BookingIntent"

    def test_event_type_name_email_received(self):
        ev = EmailReceived(
            message_id="msg-1",
            from_email="a@b.com",
            subject="Test",
            body="Hello",
            received_at=datetime.now(timezone.utc),
        )
        assert event_type_name(ev) == "EmailReceived"


class TestEventRegistry:
    def test_all_event_types_in_registry(self):
        assert len(EVENT_REGISTRY) == len(EventType)
        for et in EventType:
            assert et.value in EVENT_REGISTRY

    def test_registry_returns_correct_class(self):
        cls = EVENT_REGISTRY["BookingIntent"]
        assert cls is BookingIntent

        cls = EVENT_REGISTRY["JournalEntry"]
        assert cls is JournalEntry


class TestDeserialize:
    def test_roundtrip_booking_intent(self):
        original = BookingIntent(
            client_email="test@example.com",
            client_name="Test Client",
            dog_name="Bello",
            walk_date="2025-07-04",
            walk_slot="11:30",
            raw_message="Walk Bello Friday 11:30",
        )
        payload = original.model_dump()
        restored = deserialize_event("BookingIntent", payload)
        assert isinstance(restored, BookingIntent)
        assert restored.client_email == "test@example.com"
        assert restored.dog_name == "Bello"
        assert restored.walk_date == "2025-07-04"

    def test_deserialize_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown event type"):
            deserialize_event("NonexistentEvent", {})

    def test_roundtrip_all_event_types(self):
        """Every event type should survive a serialize → deserialize roundtrip."""
        now = datetime.now(timezone.utc)
        samples = [
            EmailReceived(message_id="m1", from_email="a@b.com", subject="s", body="b", received_at=now),
            BookingIntent(client_email="a@b.com"),
            CancellationIntent(client_email="a@b.com"),
            RescheduleIntent(client_email="a@b.com"),
            QueryIntent(client_email="a@b.com"),
            ClarificationRequest(client_email="a@b.com", intent="booking", missing_fields=["dog_name"]),
            ComplaintIntent(client_email="a@b.com"),
            ScheduleConfirmed(booking_id="b1", client_email="a@b.com", client_name="C", dog_name="D", walker_id="w1", walker_name="W", walk_date="2025-07-04", walk_slot="11:30"),
            ScheduleConflict(conflict_details="full"),
            CancellationConfirmed(booking_id="b1", client_email="a@b.com", client_name="C"),
            ScheduleUpdated(booking_id="b1", new_date="2025-07-05", new_slot="12:00", walker_id="w1", walker_name="W"),
            ConfirmationSent(to_email="a@b.com", subject="s", body="b"),
            MessageSent(to_email="a@b.com", subject="s", body="b"),
            InvoiceGenerated(invoice_id="i1", client_email="a@b.com", client_name="C", amount_eur=20.0, due_date="2025-07-10", walk_date="2025-07-04"),
            PaymentReminder(invoice_id="i1", client_email="a@b.com", client_name="C"),
            PaymentConfirmed(invoice_id="i1", client_email="a@b.com", amount_eur=20.0),
            ReminderDue(reminder_type="walk_reminder", target_email="a@b.com"),
            WalkCompleted(booking_id="b1", walker_name="W", dog_name="D"),
            HumanApprovalRequired(gate_type="new_client", context={"key": "val"}),
            HumanApproved(gate_id="g1"),
            HumanRejected(gate_id="g1"),
            JournalEntry(event_type="test", actor="agent", details={"k": "v"}),
        ]
        for ev in samples:
            type_name = event_type_name(ev)
            payload = ev.model_dump()
            restored = deserialize_event(type_name, payload)
            assert type(restored) is type(ev), f"Roundtrip failed for {type_name}"
