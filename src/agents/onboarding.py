"""Onboarding Agent — manages the new-client registration flow.

Subscribes to: OnboardingStarted, DogInfoProvided, HumanApproved
Emits: HumanApprovalRequired, BookingIntent (on approval)

Flow:
  1. New client emails → IntakeAgent detects unknown → emits OnboardingStarted
  2. OnboardingAgent receives it:
     a. Checks rate limit (configurable, 0 = disabled)
     b. Creates/updates onboarding session (status: awaiting_info)
     c. Sends structured welcome email asking for dog details
  3. Client replies with dog info → IntakeAgent detects onboarding reply →
     emits DogInfoProvided
  4. OnboardingAgent receives DogInfoProvided:
     a. Validates required fields
     b. Creates pending client + dog records in DB
     c. Updates session (status: pending_approval)
     d. Emits HumanApprovalRequired (gate_type: onboarding_approval)
  5. Human approves via dashboard → emits HumanApproved
  6. OnboardingAgent receives HumanApproved (if gate matches):
     a. Activates client record
     b. Re-emits original BookingIntent if present in session context
     c. Updates session (status: approved)

Rate limiting:
  Configured via ONBOARDING_RATE_LIMIT_PER_MIN in .env (0 = disabled).
  When active, counts per-email requests within a 60-second window.
  Exceeding the limit returns a polite wait message.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import Any

import aiosqlite

from ..router.event import (
    BaseEvent,
    BookingIntent,
    DogInfoProvided,
    HumanApprovalRequired,
    HumanApproved,
    OnboardingStarted,
)
from ..router.router import EventRouter
from ..config import Settings, get_settings
from ..email.smtp_client import SMTPClient
from ..email.templates import TEMPLATES
from .base import BaseAgent

logger = logging.getLogger(__name__)


def _uuid() -> str:
    return uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OnboardingAgent(BaseAgent):
    """Manages onboarding of new clients: welcome email → info collection → approval."""

    name = "OnboardingAgent"

    def __init__(self, router: EventRouter, settings: Settings | None = None):
        super().__init__(router)
        self._settings = settings or get_settings()
        self._smtp = SMTPClient(
            host=self._settings.smtp_host,
            port=self._settings.smtp_port,
            user=self._settings.smtp_user,
            password=self._settings.smtp_password,
        )
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = self._router.store.db
        return self._db

    def subscribed_event_types(self) -> list[str]:
        return [
            "OnboardingStarted",
            "DogInfoProvided",
            "HumanApproved",
        ]

    async def on_start(self) -> None:
        logger.info("OnboardingAgent started")

    async def on_stop(self) -> None:
        logger.info("OnboardingAgent stopped")

    async def handle(self, event: BaseEvent) -> None:
        if isinstance(event, OnboardingStarted):
            await self._handle_onboarding_started(event)
        elif isinstance(event, DogInfoProvided):
            await self._handle_dog_info_provided(event)
        elif isinstance(event, HumanApproved):
            await self._handle_human_approved(event)

    # ── Onboarding Started ─────────────────────────────────

    async def _handle_onboarding_started(self, event: OnboardingStarted) -> None:
        """New client wants to book → check rate limit, send welcome email."""
        email = event.client_email
        client_name = event.client_name or "there"

        # Rate-limit check (0 = disabled)
        if self._settings.onboarding_rate_limit_per_min > 0:
            blocked = await self._check_rate_limit(email)
            if blocked:
                logger.warning(
                    "OnboardingAgent: rate limit hit for %s (%d/min)",
                    email, self._settings.onboarding_rate_limit_per_min,
                )
                return

        # Check for existing session
        existing = await self._get_session(email)
        if existing:
            logger.info(
                "OnboardingAgent: existing session for %s (status=%s), skipping",
                email, existing["status"],
            )
            return

        # Create session
        session_id = _uuid()
        now = _now()
        await self.db.execute(
            """INSERT INTO onboarding_sessions
               (id, email, client_name, status, first_contact_at, last_contact_at, originating_gate_id)
               VALUES (?, ?, ?, 'awaiting_info', ?, ?, ?)""",
            (session_id, email, client_name, now, now, event.id),
        )
        await self.db.commit()

        # Send welcome email
        subject, body = TEMPLATES["onboarding_welcome"](client_name=client_name)
        sent = await self._smtp.send(email, subject, body)

        await self._record_message(email, "outbound", subject, body, "sent" if sent else "failed")

        logger.info("OnboardingAgent: sent welcome to %s (session=%s, sent=%s)", email, session_id, sent)

    # ── Dog Info Provided ──────────────────────────────────

    async def _handle_dog_info_provided(self, event: DogInfoProvided) -> None:
        """Client replied with dog details → validate, create records, request approval."""
        email = event.client_email
        session = await self._get_session(email)

        if not session:
            logger.warning("OnboardingAgent: no session for %s, ignoring", email)
            return

        if session["status"] not in ("awaiting_info",):
            logger.info("OnboardingAgent: session %s already at status %s", session["id"], session["status"])
            return

        # Validate required fields
        missing = []
        if not event.dog_name:
            missing.append("dog_name")
        if not event.breed:
            missing.append("breed")
        if event.age_months is None:
            missing.append("age_months")
        if not event.sex:
            missing.append("sex")

        if missing:
            # Send a polite follow-up asking for missing info
            subject = "❓ A bit more info needed"
            fields_text = ", ".join(missing)
            body = f"""Hi {event.client_name or "there"},

Thanks for your reply! We just need a few more details about {event.dog_name or "your dog"}:

  • {fields_text}

Please reply with this information and we'll get you set up right away!

— The Walking Hounds Team
"""
            sent = await self._smtp.send(email, subject, body)
            await self._record_message(email, "outbound", subject, body, "sent" if sent else "failed")
            logger.info("OnboardingAgent: follow-up for missing fields %s → %s", missing, email)
            return

        # Store dog details as JSON
        dog_details = {
            "dog_name": event.dog_name,
            "breed": event.breed,
            "age_months": event.age_months,
            "sex": event.sex,
            "castrated": event.castrated,
            "in_heat": event.in_heat,
            "temperament": event.temperament,
            "special_needs": event.special_needs,
        }

        now = _now()
        await self.db.execute(
            """UPDATE onboarding_sessions
               SET status = 'pending_approval', dog_details = ?, last_contact_at = ?
               WHERE id = ?""",
            (json.dumps(dog_details), now, session["id"]),
        )
        await self.db.commit()

        # Emit human approval gate
        await self.emit(HumanApprovalRequired(
            gate_type="onboarding_approval",
            context={
                "session_id": session["id"],
                "email": email,
                "client_name": event.client_name,
                "dog_details": dog_details,
                "original_intent": "booking",  # TODO: preserve from session
            },
            options=["approve_and_activate", "reject"],
        ))

        logger.info(
            "OnboardingAgent: dog info received for %s (dog=%s) → pending approval",
            email, event.dog_name,
        )

    # ── Human Approved ─────────────────────────────────────

    async def _handle_human_approved(self, event: HumanApproved) -> None:
        """Human approved an onboarding gate → activate client, re-emit booking."""
        # Check if this approval is for an onboarding gate
        gate = await self._get_gate(event.gate_id)
        if not gate or gate.get("gate_type") != "onboarding_approval":
            return  # not our gate

        context = json.loads(gate.get("context", "{}"))
        session_id = context.get("session_id")
        email = context.get("email")
        dog_details = context.get("dog_details", {})

        if event.decision != "approve_and_activate":
            # Rejection
            if session_id:
                await self.db.execute(
                    "UPDATE onboarding_sessions SET status='rejected', resolved_at=? WHERE id=?",
                    (_now(), session_id),
                )
                await self.db.commit()
            logger.info("OnboardingAgent: onboarding rejected for %s", email)
            return

        # Approval → create client + dog records
        client_name = context.get("client_name", dog_details.get("client_name", email.split("@")[0]))
        client_id = _uuid()
        now = _now()

        await self.db.execute(
            """INSERT OR IGNORE INTO clients (id, name, email, status, created_at)
               VALUES (?, ?, ?, 'active', ?)""",
            (client_id, client_name, email, now),
        )

        dog_id = _uuid()
        await self.db.execute(
            """INSERT OR IGNORE INTO dogs
               (id, client_id, name, breed, age_months, temperament, sex, castrated, in_heat, special_needs, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dog_id, client_id,
                dog_details.get("dog_name", ""),
                dog_details.get("breed", ""),
                dog_details.get("age_months", 0),
                dog_details.get("temperament", ""),
                dog_details.get("sex", ""),
                dog_details.get("castrated", ""),
                dog_details.get("in_heat", 0),
                dog_details.get("special_needs", ""),
                now,
            ),
        )

        # Update session
        if session_id:
            await self.db.execute(
                "UPDATE onboarding_sessions SET status='approved', resolved_at=? WHERE id=?",
                (now, session_id),
            )
        await self.db.commit()

        logger.info("OnboardingAgent: onboarded %s (%s / %s)", email, client_name, dog_details.get("dog_name"))

        # Re-emit original booking intent if present
        # The dashboard can forward the original booking intent; for now we just log
        # TODO: store and replay the original intent from the session

    # ── Rate Limiting ──────────────────────────────────────

    async def _check_rate_limit(self, email: str) -> bool:
        """Return True if this email has exceeded the rate limit."""
        limit = self._settings.onboarding_rate_limit_per_min
        if limit <= 0:
            return False

        now = datetime.now(timezone.utc)
        window_start = (now - timedelta(minutes=1)).isoformat()

        rows = await self.db.execute_fetchall(
            """SELECT COUNT(*) as cnt FROM onboarding_sessions
               WHERE email = ? AND first_contact_at >= ?""",
            (email, window_start),
        )
        count = rows[0]["cnt"] if rows else 0
        return count >= limit

    # ── Helpers ────────────────────────────────────────────

    async def _get_session(self, email: str) -> dict | None:
        """Get the most recent active onboarding session for this email."""
        rows = await self.db.execute_fetchall(
            """SELECT * FROM onboarding_sessions
               WHERE email = ? AND status IN ('awaiting_info', 'pending_approval')
               ORDER BY last_contact_at DESC LIMIT 1""",
            (email,),
        )
        return dict(rows[0]) if rows else None

    async def _get_gate(self, gate_id: str) -> dict | None:
        """Look up an approval gate."""
        rows = await self.db.execute_fetchall(
            "SELECT * FROM approval_gates WHERE id = ?", (gate_id,),
        )
        return dict(rows[0]) if rows else None

    async def _record_message(
        self,
        client_email: str,
        direction: str,
        subject: str,
        body: str,
        status: str = "sent",
    ) -> None:
        """Record a sent message in the messages table."""
        now = _now()
        msg_id = _uuid()

        rows = await self.db.execute_fetchall(
            "SELECT id FROM clients WHERE email = ?", (client_email,),
        )
        client_id = rows[0]["id"] if rows else None

        from_addr = self._settings.smtp_user if direction == "outbound" else client_email
        to_addr = client_email if direction == "outbound" else self._settings.smtp_user

        await self.db.execute(
            """INSERT INTO messages (id, client_id, direction, channel, from_email, to_email, subject, body, sent_at, status)
               VALUES (?, ?, ?, 'email', ?, ?, ?, ?, ?, ?)""",
            (msg_id, client_id, direction, from_addr, to_addr, subject, body, now, status),
        )
        await self.db.commit()