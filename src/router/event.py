"""All typed events for the Walking Hounds event router.

Every event is a frozen Pydantic model.  Events are the *only* way
agents communicate — no direct calls between agents.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid4().hex


# ── Base ────────────────────────────────────────────────────

class BaseEvent(BaseModel):
    """Root event type.  Every event has an id, timestamp, and optional
    correlation id for tracing through the system."""

    id: str = Field(default_factory=_uuid)
    created_at: datetime = Field(default_factory=_utcnow)
    correlation_id: str | None = None

    model_config = {"frozen": True}


# ── Intake events ──────────────────────────────────────────

class EmailReceived(BaseEvent):
    """Raw email pulled from IMAP — triggers Intake Agent."""
    message_id: str
    from_email: str
    subject: str
    body: str
    received_at: datetime


class BookingIntent(BaseEvent):
    """Client wants to book a walk."""
    client_email: str
    client_name: str | None = None
    dog_name: str | None = None
    walk_date: str | None = None      # ISO date
    walk_slot: str | None = None       # "11:30" etc
    confidence: float = 1.0
    raw_message: str = ""


class CancellationIntent(BaseEvent):
    """Client wants to cancel a walk."""
    client_email: str
    client_name: str | None = None
    booking_ref: str | None = None
    walk_date: str | None = None
    reason: str = ""
    raw_message: str = ""


class RescheduleIntent(BaseEvent):
    """Client wants to move a walk to a different day/slot."""
    client_email: str
    client_name: str | None = None
    booking_ref: str | None = None
    original_date: str | None = None
    new_date: str | None = None
    new_slot: str | None = None
    raw_message: str = ""


class QueryIntent(BaseEvent):
    """General question from a client."""
    client_email: str
    client_name: str | None = None
    query_text: str = ""
    suggested_response: str = ""
    raw_message: str = ""


class ComplaintIntent(BaseEvent):
    """Client is unhappy — routes to human gate."""
    client_email: str
    client_name: str | None = None
    complaint_text: str = ""
    severity: str = "medium"   # low / medium / high
    raw_message: str = ""


# ── Scheduling events ──────────────────────────────────────

class ScheduleConfirmed(BaseEvent):
    """A walk has been scheduled and assigned."""
    booking_id: str
    client_email: str
    client_name: str
    dog_name: str
    walker_id: str
    walker_name: str
    walk_date: str
    walk_slot: str
    group_id: str | None = None


class ScheduleConflict(BaseEvent):
    """Scheduling Agent couldn't auto-resolve — needs human."""
    conflict_details: str
    alternatives: list[dict] = Field(default_factory=list)
    original_intent_event_id: str | None = None


class CancellationConfirmed(BaseEvent):
    """A walk has been cancelled."""
    booking_id: str
    client_email: str
    client_name: str
    refund_percent: int = 0
    late_cancellation: bool = False


class ScheduleUpdated(BaseEvent):
    """Walk rescheduled or walker reassigned."""
    booking_id: str
    old_date: str | None = None
    old_slot: str | None = None
    new_date: str
    new_slot: str
    walker_id: str
    walker_name: str


# ── Communication events ───────────────────────────────────

class ConfirmationSent(BaseEvent):
    """Booking confirmation email sent."""
    to_email: str
    subject: str
    body: str
    booking_id: str | None = None


class MessageSent(BaseEvent):
    """General outbound message sent."""
    to_email: str
    subject: str
    body: str
    message_type: str = "general"


# ── Invoicing events ───────────────────────────────────────

class InvoiceGenerated(BaseEvent):
    """Invoice created and ready to send."""
    invoice_id: str
    client_email: str
    client_name: str
    amount_eur: float
    due_date: str
    walk_date: str
    booking_id: str | None = None


class PaymentReminder(BaseEvent):
    """Automated payment reminder."""
    invoice_id: str
    client_email: str
    client_name: str
    reminder_count: int = 1
    amount_eur: float = 0.0


class PaymentConfirmed(BaseEvent):
    """Human marked invoice as paid via dashboard."""
    invoice_id: str
    client_email: str
    amount_eur: float


# ── Reminder events ────────────────────────────────────────

class ReminderDue(BaseEvent):
    """Time-based reminder fired (walk reminder, feedback request)."""
    reminder_type: str     # walk_reminder / walker_briefing / feedback
    target_email: str
    booking_id: str | None = None
    walk_date: str | None = None
    walk_slot: str | None = None


class WalkCompleted(BaseEvent):
    """A walk has been completed (timer or human-triggered)."""
    booking_id: str
    walker_name: str
    dog_name: str
    duration_min: int = 60
    notes: str = ""


# ── Human-gate events ──────────────────────────────────────

class HumanApprovalRequired(BaseEvent):
    """A workflow branch is paused waiting for human decision."""
    gate_type: str    # new_client / ambiguous_intent / schedule_conflict /
                       # complaint_response / unusual_request / payment_escalation
    context: dict
    options: list[str] = Field(default_factory=list)
    originating_event_id: str | None = None


class HumanApproved(BaseEvent):
    """Human approved a gate — resumes the workflow."""
    gate_id: str
    decision: str = "approved"
    notes: str = ""
    resolved_by: str = "human"


class HumanRejected(BaseEvent):
    """Human rejected a gate."""
    gate_id: str
    reason: str = ""
    resolved_by: str = "human"


# ── Journal ────────────────────────────────────────────────

class JournalEntry(BaseEvent):
    """Logger Agent records this — terminal sink, no further processing."""
    event_type: str
    actor: str          # agent name or "human"
    details: dict
    related_booking_id: str | None = None
    related_client_id: str | None = None


# ── Registry ───────────────────────────────────────────────

class EventType(str, Enum):
    """String enum for event type names — used by the router for routing."""
    EMAIL_RECEIVED = "EmailReceived"
    BOOKING_INTENT = "BookingIntent"
    CANCELLATION_INTENT = "CancellationIntent"
    RESCHEDULE_INTENT = "RescheduleIntent"
    QUERY_INTENT = "QueryIntent"
    COMPLAINT_INTENT = "ComplaintIntent"
    SCHEDULE_CONFIRMED = "ScheduleConfirmed"
    SCHEDULE_CONFLICT = "ScheduleConflict"
    CANCELLATION_CONFIRMED = "CancellationConfirmed"
    SCHEDULE_UPDATED = "ScheduleUpdated"
    CONFIRMATION_SENT = "ConfirmationSent"
    MESSAGE_SENT = "MessageSent"
    INVOICE_GENERATED = "InvoiceGenerated"
    PAYMENT_REMINDER = "PaymentReminder"
    PAYMENT_CONFIRMED = "PaymentConfirmed"
    REMINDER_DUE = "ReminderDue"
    WALK_COMPLETED = "WalkCompleted"
    HUMAN_APPROVAL_REQUIRED = "HumanApprovalRequired"
    HUMAN_APPROVED = "HumanApproved"
    HUMAN_REJECTED = "HumanRejected"
    JOURNAL_ENTRY = "JournalEntry"


EVENT_REGISTRY: dict[str, type[BaseEvent]] = {
    EventType.EMAIL_RECEIVED.value: EmailReceived,
    EventType.BOOKING_INTENT.value: BookingIntent,
    EventType.CANCELLATION_INTENT.value: CancellationIntent,
    EventType.RESCHEDULE_INTENT.value: RescheduleIntent,
    EventType.QUERY_INTENT.value: QueryIntent,
    EventType.COMPLAINT_INTENT.value: ComplaintIntent,
    EventType.SCHEDULE_CONFIRMED.value: ScheduleConfirmed,
    EventType.SCHEDULE_CONFLICT.value: ScheduleConflict,
    EventType.CANCELLATION_CONFIRMED.value: CancellationConfirmed,
    EventType.SCHEDULE_UPDATED.value: ScheduleUpdated,
    EventType.CONFIRMATION_SENT.value: ConfirmationSent,
    EventType.MESSAGE_SENT.value: MessageSent,
    EventType.INVOICE_GENERATED.value: InvoiceGenerated,
    EventType.PAYMENT_REMINDER.value: PaymentReminder,
    EventType.PAYMENT_CONFIRMED.value: PaymentConfirmed,
    EventType.REMINDER_DUE.value: ReminderDue,
    EventType.WALK_COMPLETED.value: WalkCompleted,
    EventType.HUMAN_APPROVAL_REQUIRED.value: HumanApprovalRequired,
    EventType.HUMAN_APPROVED.value: HumanApproved,
    EventType.HUMAN_REJECTED.value: HumanRejected,
    EventType.JOURNAL_ENTRY.value: JournalEntry,
}


def event_type_name(event: BaseEvent) -> str:
    """Return the string type name for an event instance."""
    return type(event).__name__


def deserialize_event(type_name: str, payload: dict) -> BaseEvent:
    """Reconstruct an event from its type name and JSON payload."""
    cls = EVENT_REGISTRY.get(type_name)
    if cls is None:
        raise ValueError(f"Unknown event type: {type_name}")
    return cls.model_validate(payload)
