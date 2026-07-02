"""Logger Agent — subscribes to ALL events and writes them to the journal table.

This is the system's audit trail. Every event that flows through the router
is recorded here with its type, timestamp, actor (agent name), and details.

Subscribes to: * (wildcard — all events)
Emits: nothing (terminal sink)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from ..router.event import (
    BaseEvent,
    HumanApprovalRequired,
    JournalEntry,
    ScheduleConfirmed,
    CancellationConfirmed,
    ScheduleUpdated,
    InvoiceGenerated,
    PaymentConfirmed,
    PaymentReminder,
    ConfirmationSent,
    MessageSent,
    ReminderDue,
    WalkCompleted,
    BookingIntent,
    CancellationIntent,
    RescheduleIntent,
    QueryIntent,
    ComplaintIntent,
    ClarificationRequest,
    EmailReceived,
    HumanApproved,
    HumanRejected,
)
from ..router.router import EventRouter
from .base import BaseAgent

logger = logging.getLogger(__name__)


# Maps event types to a human-readable actor and action label.
_EVENT_META: dict[type, tuple[str, str]] = {
    EmailReceived:        ("IMAP",           "Email received"),
    BookingIntent:        ("IntakeAgent",    "Booking intent parsed"),
    CancellationIntent:   ("IntakeAgent",    "Cancellation intent parsed"),
    RescheduleIntent:     ("IntakeAgent",    "Reschedule intent parsed"),
    QueryIntent:          ("IntakeAgent",    "Query intent parsed"),
    ComplaintIntent:      ("IntakeAgent",    "Complaint intent detected"),
    ClarificationRequest: ("IntakeAgent",    "Clarification needed"),
    ScheduleConfirmed:    ("SchedulingAgent","Walk scheduled"),
    ScheduleUpdated:      ("SchedulingAgent","Walk rescheduled"),
    CancellationConfirmed:("SchedulingAgent","Walk cancelled"),
    ConfirmationSent:     ("CommunicationAgent", "Confirmation sent"),
    MessageSent:          ("CommunicationAgent", "Message sent"),
    InvoiceGenerated:     ("InvoicingAgent", "Invoice generated"),
    PaymentReminder:      ("InvoicingAgent", "Payment reminder"),
    PaymentConfirmed:     ("InvoicingAgent", "Payment confirmed"),
    ReminderDue:          ("ReminderAgent",  "Reminder triggered"),
    WalkCompleted:        ("ReminderAgent",  "Walk completed"),
    HumanApprovalRequired:("System",         "Human approval required"),
    HumanApproved:        ("Human",          "Gate approved"),
    HumanRejected:        ("Human",          "Gate rejected"),
    JournalEntry:         ("LoggerAgent",    "Journal entry"),
}


def _uuid() -> str:
    import uuid
    return uuid.uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LoggerAgent(BaseAgent):
    """Records every event to the journal table — the audit trail."""

    name = "LoggerAgent"

    def __init__(self, router: EventRouter):
        super().__init__(router)
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = self._router.store.db
        return self._db

    def subscribed_event_types(self) -> list[str]:
        return ["*"]  # Wildcard — receive everything

    async def handle(self, event: BaseEvent) -> None:
        meta = _EVENT_META.get(type(event), ("Unknown", type(event).__name__))
        actor, action = meta

        # Build details dict from the event
        details: dict[str, Any] = {}
        try:
            details = json.loads(event.model_dump_json())
        except Exception:
            details = {"raw": str(event)}

        # Extract related IDs
        related_booking = getattr(event, "booking_id", None)
        related_client = getattr(event, "client_email", None)

        await self.db.execute(
            """INSERT INTO journal (id, event_type, timestamp, actor, details,
                                    related_booking_id, related_client_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                _uuid(),
                type(event).__name__,
                _now(),
                actor,
                json.dumps(details, ensure_ascii=False, default=str),
                related_booking,
                related_client,
            ),
        )
        await self.db.commit()

    # ── Query helpers for dashboard ─────────────────────────

    async def get_recent_entries(self, limit: int = 50) -> list[dict]:
        """Return the most recent journal entries for the dashboard."""
        rows = await self.db.execute_fetchall(
            """SELECT j.*, 
                      (SELECT action FROM (
                          SELECT 'Email received' as action, 'EmailReceived' as et
                          UNION ALL SELECT 'Booking intent', 'BookingIntent'
                          UNION ALL SELECT 'Cancellation intent', 'CancellationIntent'
                          UNION ALL SELECT 'Reschedule intent', 'RescheduleIntent'
                          UNION ALL SELECT 'Query intent', 'QueryIntent'
                          UNION ALL SELECT 'Complaint detected', 'ComplaintIntent'
                          UNION ALL SELECT 'Clarification needed', 'ClarificationRequest'
                          UNION ALL SELECT 'Walk scheduled', 'ScheduleConfirmed'
                          UNION ALL SELECT 'Walk rescheduled', 'ScheduleUpdated'
                          UNION ALL SELECT 'Walk cancelled', 'CancellationConfirmed'
                          UNION ALL SELECT 'Confirmation sent', 'ConfirmationSent'
                          UNION ALL SELECT 'Message sent', 'MessageSent'
                          UNION ALL SELECT 'Invoice generated', 'InvoiceGenerated'
                          UNION ALL SELECT 'Payment reminder', 'PaymentReminder'
                          UNION ALL SELECT 'Payment confirmed', 'PaymentConfirmed'
                          UNION ALL SELECT 'Reminder triggered', 'ReminderDue'
                          UNION ALL SELECT 'Walk completed', 'WalkCompleted'
                          UNION ALL SELECT 'Human approval required', 'HumanApprovalRequired'
                          UNION ALL SELECT 'Gate approved', 'HumanApproved'
                          UNION ALL SELECT 'Gate rejected', 'HumanRejected'
                      ) WHERE et = j.event_type
                      ) as action_label
               FROM journal j
               ORDER BY j.timestamp DESC
               LIMIT ?""",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def get_entry_count(self) -> int:
        rows = await self.db.execute_fetchall(
            "SELECT COUNT(*) as cnt FROM journal"
        )
        return rows[0]["cnt"] if rows else 0
