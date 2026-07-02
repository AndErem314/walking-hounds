"""Communication Agent — handles all outbound messages.

Subscribes to: ScheduleConfirmed, CancellationConfirmed, ScheduleUpdated,
               ClarificationRequest, QueryIntent, ComplaintIntent,
               ReminderDue, PaymentReminder, HumanApprovedResponse
Emits: ConfirmationSent, MessageSent, HumanApprovalRequired

Key behaviors:
- Renders templates for confirmations, cancellations, reminders
- Sends clarification requests when emails were unclear
- Drafts LLM responses to general queries
- Detects negative sentiment in complaints → human gate for response approval
- Records all sent messages in the messages table
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4
from typing import Any

import aiosqlite

from ..router.event import (
    BaseEvent,
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
from ..router.router import EventRouter
from ..config import Settings, get_settings
from ..email.smtp_client import SMTPClient
from ..email.templates import TEMPLATES
from ..llm.ollama_client import OllamaClient
from .base import BaseAgent

logger = logging.getLogger(__name__)

_NEGATIVE_KEYWORDS = [
    "late", "angry", "unacceptable", "terrible", "awful", "complaint",
    "disappointed", "frustrated", "horrible", "worst", "never again",
    "refund", "compensation", "legal", "sue", "authorities",
]


class CommunicationAgent(BaseAgent):
    """Handles all outbound email communication."""

    name = "CommunicationAgent"

    def __init__(self, router: EventRouter, settings: Settings | None = None):
        super().__init__(router)
        self._settings = settings or get_settings()
        self._smtp = SMTPClient(
            host=self._settings.smtp_host,
            port=self._settings.smtp_port,
            user=self._settings.smtp_user,
            password=self._settings.smtp_password,
        )
        self._ollama = OllamaClient(
            host=self._settings.ollama_host,
            model=self._settings.ollama_model,
        )
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = self._router.store.db
        return self._db

    def subscribed_event_types(self) -> list[str]:
        return [
            "ScheduleConfirmed",
            "CancellationConfirmed",
            "ScheduleUpdated",
            "ClarificationRequest",
            "QueryIntent",
            "ComplaintIntent",
            "ReminderDue",
            "PaymentReminder",
        ]

    async def on_start(self) -> None:
        await self._ollama.init()
        logger.info("CommunicationAgent started")

    async def on_stop(self) -> None:
        await self._ollama.close()
        logger.info("CommunicationAgent stopped")

    async def handle(self, event: BaseEvent) -> None:
        if isinstance(event, ScheduleConfirmed):
            await self._handle_booking_confirmation(event)
        elif isinstance(event, CancellationConfirmed):
            await self._handle_cancellation_confirmation(event)
        elif isinstance(event, ScheduleUpdated):
            await self._handle_reschedule_confirmation(event)
        elif isinstance(event, ClarificationRequest):
            await self._handle_clarification(event)
        elif isinstance(event, QueryIntent):
            await self._handle_query(event)
        elif isinstance(event, ComplaintIntent):
            await self._handle_complaint(event)
        elif isinstance(event, ReminderDue):
            await self._handle_reminder(event)
        elif isinstance(event, PaymentReminder):
            await self._handle_payment_reminder(event)

    # ── Booking Confirmation ────────────────────────────────

    async def _handle_booking_confirmation(self, event: ScheduleConfirmed) -> None:
        subject, body = TEMPLATES["booking_confirmation"](
            client_name=event.client_name,
            dog_name=event.dog_name,
            walker_name=event.walker_name,
            walk_date=event.walk_date,
            walk_slot=event.walk_slot,
            price_eur=self._settings.walk_price_eur,
            payment_address=self._settings.invoice_payment_address,
        )

        sent = await self._smtp.send(event.client_email, subject, body)

        await self._record_message(
            client_email=event.client_email,
            direction="outbound",
            subject=subject,
            body=body,
            status="sent" if sent else "failed",
        )

        if sent:
            await self.emit(ConfirmationSent(
                to_email=event.client_email,
                subject=subject,
                body=body,
                booking_id=event.booking_id,
            ))

    # ── Cancellation Confirmation ───────────────────────────

    async def _handle_cancellation_confirmation(self, event: CancellationConfirmed) -> None:
        subject, body = TEMPLATES["cancellation_confirmation"](
            client_name=event.client_name,
            dog_name="",  # We don't have dog name in CancellationConfirmed
            walk_date="",
            refund_percent=event.refund_percent,
            late_cancellation=event.late_cancellation,
        )

        sent = await self._smtp.send(event.client_email, subject, body)

        await self._record_message(
            client_email=event.client_email,
            direction="outbound",
            subject=subject,
            body=body,
            status="sent" if sent else "failed",
        )

        if sent:
            await self.emit(MessageSent(
                to_email=event.client_email,
                subject=subject,
                body=body,
                message_type="cancellation_confirmation",
            ))

    # ── Reschedule Confirmation ─────────────────────────────

    async def _handle_reschedule_confirmation(self, event: ScheduleUpdated) -> None:
        # We need client email — look up from walk
        client_email = await self._get_walk_client_email(event.booking_id)
        if not client_email:
            logger.warning("CommunicationAgent: no client email for walk %s", event.booking_id)
            return

        subject, body = TEMPLATES["reschedule_confirmation"](
            client_name="",
            dog_name="",
            old_date=event.old_date or "",
            new_date=event.new_date,
            new_slot=event.new_slot,
            walker_name=event.walker_name,
        )

        sent = await self._smtp.send(client_email, subject, body)

        await self._record_message(
            client_email=client_email,
            direction="outbound",
            subject=subject,
            body=body,
            status="sent" if sent else "failed",
        )

        if sent:
            await self.emit(MessageSent(
                to_email=client_email,
                subject=subject,
                body=body,
                message_type="reschedule_confirmation",
            ))

    # ── Clarification Request ───────────────────────────────

    async def _handle_clarification(self, event: ClarificationRequest) -> None:
        client_name = event.client_name or "there"
        subject, body = TEMPLATES["clarification_request"](
            client_name=client_name,
            clarification_text=event.suggested_clarification,
        )

        sent = await self._smtp.send(event.client_email, subject, body)

        await self._record_message(
            client_email=event.client_email,
            direction="outbound",
            subject=subject,
            body=body,
            status="sent" if sent else "failed",
        )

        if sent:
            await self.emit(MessageSent(
                to_email=event.client_email,
                subject=subject,
                body=body,
                message_type="clarification_request",
            ))

    # ── Query Response (LLM-drafted) ────────────────────────

    async def _handle_query(self, event: QueryIntent) -> None:
        """Draft a response to a general query using the LLM."""
        draft = await self._draft_query_response(event.query_text)

        subject = "Re: Your question"
        body = f"""Hi {event.client_name or "there"},

{draft}

— The Walking Hounds Team
"""

        sent = await self._smtp.send(event.client_email, subject, body)

        await self._record_message(
            client_email=event.client_email,
            direction="outbound",
            subject=subject,
            body=body,
            status="sent" if sent else "failed",
        )

        if sent:
            await self.emit(MessageSent(
                to_email=event.client_email,
                subject=subject,
                body=body,
                message_type="query_response",
            ))

    async def _draft_query_response(self, query_text: str) -> str:
        """Use Ollama to draft a polite response to a client query."""
        prompt = f"""You are a friendly assistant for a dog-walking business called Walking Hounds.
A client sent this question:

{query_text[:1000]}

Write a short, friendly, helpful response (2-3 sentences max).
If the question is about pricing, the price is €20 per walk.
If you don't know the answer, suggest they reply for more details.
Do not include greetings or sign-offs — just the response text."""

        try:
            response = await self._ollama.generate(prompt, temperature=0.4)
            return response.strip()
        except Exception as exc:
            logger.error("CommunicationAgent: LLM draft failed: %s", exc)
            return "Thank you for your question! We'll get back to you with more details soon."

    # ── Complaint (Human Gate) ──────────────────────────────

    async def _handle_complaint(self, event: ComplaintIntent) -> None:
        """Complaints always go through human review before sending a response."""
        # Draft an empathetic response
        draft = await self._draft_complaint_response(event.complaint_text, event.severity)

        await self.emit(HumanApprovalRequired(
            gate_type="complaint_response",
            context={
                "client_email": event.client_email,
                "client_name": event.client_name,
                "complaint_text": event.complaint_text[:500],
                "severity": event.severity,
                "drafted_response": draft,
            },
            options=["approve", "edit", "reject"],
            originating_event_id=event.id,
        ))

        logger.info(
            "CommunicationAgent: complaint from %s (severity=%s) → human gate",
            event.client_email, event.severity,
        )

    async def _draft_complaint_response(self, complaint_text: str, severity: str) -> str:
        """Draft an empathetic response to a complaint."""
        prompt = f"""You are a customer service assistant for a dog-walking business called Walking Hounds.
A client has submitted a complaint (severity: {severity}):

{complaint_text[:1000]}

Write an empathetic, professional response that:
1. Acknowledges their concern
2. Apologizes sincerely
3. Offers to investigate and follow up
4. Does NOT make promises or admit fault

Keep it to 3-4 sentences. Do not include greetings or sign-offs."""

        try:
            response = await self._ollama.generate(prompt, temperature=0.5)
            return response.strip()
        except Exception as exc:
            logger.error("CommunicationAgent: complaint draft failed: %s", exc)
            return "I'm very sorry to hear about your experience. We take your feedback seriously and will look into this matter right away. We'll follow up with you shortly."

    @staticmethod
    def _detect_negative_sentiment(text: str) -> bool:
        """Simple keyword-based negative sentiment detection."""
        text_lower = text.lower()
        return any(keyword in text_lower for keyword in _NEGATIVE_KEYWORDS)

    # ── Reminder ────────────────────────────────────────────

    async def _handle_reminder(self, event: ReminderDue) -> None:
        """Send a walk reminder or feedback request based on reminder_type."""
        if event.reminder_type == "walk_reminder":
            # Look up walk details
            walk = await self._get_walk_details(event.booking_id) if event.booking_id else None
            if not walk:
                return

            subject, body = TEMPLATES["walk_reminder"](
                client_name=walk.get("client_name", ""),
                dog_name=walk.get("dog_name", ""),
                walker_name=walk.get("walker_name", ""),
                walk_date=event.walk_date or walk.get("date", ""),
                walk_slot=event.walk_slot or walk.get("slot", ""),
            )

        elif event.reminder_type == "feedback":
            walk = await self._get_walk_details(event.booking_id) if event.booking_id else None
            if not walk:
                return

            subject, body = TEMPLATES["feedback_request"](
                client_name=walk.get("client_name", ""),
                dog_name=walk.get("dog_name", ""),
                walk_date=event.walk_date or walk.get("date", ""),
            )

        else:
            logger.warning("CommunicationAgent: unknown reminder type '%s'", event.reminder_type)
            return

        sent = await self._smtp.send(event.target_email, subject, body)

        await self._record_message(
            client_email=event.target_email,
            direction="outbound",
            subject=subject,
            body=body,
            status="sent" if sent else "failed",
        )

        if sent:
            await self.emit(MessageSent(
                to_email=event.target_email,
                subject=subject,
                body=body,
                message_type=event.reminder_type,
            ))

    # ── Payment Reminder ────────────────────────────────────

    async def _handle_payment_reminder(self, event: PaymentReminder) -> None:
        subject, body = TEMPLATES["payment_reminder"](
            client_name=event.client_name,
            amount_eur=event.amount_eur,
            due_date="soon",
            payment_address=self._settings.invoice_payment_address,
            reminder_count=event.reminder_count,
        )

        sent = await self._smtp.send(event.client_email, subject, body)

        await self._record_message(
            client_email=event.client_email,
            direction="outbound",
            subject=subject,
            body=body,
            status="sent" if sent else "failed",
        )

        if sent:
            await self.emit(MessageSent(
                to_email=event.client_email,
                subject=subject,
                body=body,
                message_type="payment_reminder",
            ))

    # ── Helpers ─────────────────────────────────────────────

    async def _record_message(
        self,
        *,
        client_email: str,
        direction: str,
        subject: str,
        body: str,
        status: str = "sent",
    ) -> None:
        """Record a sent/received message in the messages table."""
        now = datetime.now(timezone.utc).isoformat()
        msg_id = uuid4().hex

        # Look up client_id
        rows = await self.db.execute_fetchall(
            "SELECT id FROM clients WHERE email = ?", (client_email,)
        )
        client_id = rows[0]["id"] if rows else None

        await self.db.execute(
            """INSERT INTO messages (id, client_id, direction, channel, from_email, to_email, subject, body, sent_at, status)
               VALUES (?, ?, ?, 'email', ?, ?, ?, ?, ?, ?)""",
            (msg_id, client_id, direction,
             self._settings.smtp_user if direction == "outbound" else client_email,
             client_email if direction == "outbound" else self._settings.smtp_user,
             subject, body, now, status),
        )
        await self.db.commit()

    async def _get_walk_client_email(self, walk_id: str) -> str | None:
        """Look up the client email for a walk."""
        rows = await self.db.execute_fetchall(
            """SELECT c.email FROM walks w
               JOIN clients c ON w.client_id = c.id
               WHERE w.id = ?""",
            (walk_id,),
        )
        return rows[0]["email"] if rows else None

    async def _get_walk_details(self, walk_id: str) -> dict | None:
        """Look up full walk details including client and dog names."""
        rows = await self.db.execute_fetchall(
            """SELECT w.*, c.name as client_name, c.email as client_email,
                      d.name as dog_name, d.breed as breed,
                      wl.name as walker_name
               FROM walks w
               JOIN clients c ON w.client_id = c.id
               JOIN dogs d ON w.dog_id = d.id
               JOIN walkers wl ON w.walker_id = wl.id
               WHERE w.id = ?""",
            (walk_id,),
        )
        return dict(rows[0]) if rows else None
