"""Intake Agent — polls IMAP, parses emails with LLM, emits typed events.

Subscribes to: (nothing — it's the entry point, triggered by IMAP polling)
Emits: BookingIntent, CancellationIntent, RescheduleIntent, QueryIntent,
       ComplaintIntent, HumanApprovalRequired

Lifecycle:
  1. Poll IMAP every N seconds (configurable)
  2. For each unseen email:
     a. Check if message_id already processed (dedup via SQLite)
     b. Send email body to Ollama with a structured prompt
     c. Parse LLM JSON response → intent + extracted fields
     d. If confidence < threshold → emit HumanApprovalRequired
     e. If client unknown → emit HumanApprovalRequired (new_client)
     f. Otherwise → emit the typed intent event
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from ..router.event import (
    BookingIntent,
    CancellationIntent,
    ComplaintIntent,
    HumanApprovalRequired,
    QueryIntent,
    RescheduleIntent,
)
from ..router.router import EventRouter
from .base import BaseAgent
from ..config import Settings, get_settings
from ..llm.ollama_client import OllamaClient

logger = logging.getLogger(__name__)


# ── LLM Prompt ─────────────────────────────────────────────

SYSTEM_PROMPT = """You are an email parser for a dog-walking business called Walking Hounds.
You receive emails from clients and must classify them into one of these intents:

1. booking — Client wants to schedule a new walk
2. cancellation — Client wants to cancel an existing walk
3. reschedule — Client wants to move a walk to a different day/time
4. query — General question (pricing, availability, policies)
5. complaint — Client is unhappy about something
6. other — Doesn't fit any category

Extract these fields if present:
- client_name: The sender's name (first + last)
- dog_name: The dog's name mentioned
- walk_date: The requested date (ISO format YYYY-MM-DD, or null if unknown)
- walk_slot: Preferred time slot if mentioned ("11:30", "12:00", "12:30", or null)
- reason: For cancellations/complaints, the stated reason
- severity: For complaints only — "low", "medium", or "high"

Respond as JSON only, no markdown:
{
  "intent": "booking|cancellation|reschedule|query|complaint|other",
  "confidence": 0.0-1.0,
  "client_name": "string or null",
  "dog_name": "string or null",
  "walk_date": "YYYY-MM-DD or null",
  "walk_slot": "HH:MM or null",
  "reason": "string or null",
  "severity": "low|medium|high or null",
  "summary": "one-sentence summary of the email"
}"""

USER_PROMPT_TEMPLATE = """Parse this email for the dog-walking business.

From: {from_email}
Subject: {subject}

Body:
{body}

Respond as JSON only."""


class IntakeAgent(BaseAgent):
    """Polls IMAP inbox, classifies emails with LLM, emits typed events."""

    name = "IntakeAgent"

    def __init__(self, router: EventRouter, settings: Settings | None = None):
        super().__init__(router)
        self._settings = settings or get_settings()
        self._ollama = OllamaClient(
            host=self._settings.ollama_host,
            model=self._settings.ollama_model,
        )
        self._poll_interval = self._settings.imap_poll_interval_sec
        self._confidence_threshold = self._settings.intake_confidence_threshold
        self._poll_task: asyncio.Task | None = None

    def subscribed_event_types(self) -> list[str]:
        # Intake doesn't subscribe to events — it polls IMAP
        return []

    async def on_start(self) -> None:
        await self._ollama.init()
        # Start the IMAP polling loop as a background task
        self._poll_task = asyncio.create_task(self._poll_loop(), name="intake-imap-poll")
        logger.info("IntakeAgent started — polling IMAP every %ds", self._poll_interval)

    async def on_stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await asyncio.wait_for(self._poll_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        await self._ollama.close()
        logger.info("IntakeAgent stopped")

    async def handle(self, event) -> None:
        # Intake doesn't receive events — it's the entry point
        pass

    # ── IMAP Polling ────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main polling loop — runs until cancelled."""
        from ..email.imap_client import IMAPClient

        while True:
            try:
                imap = IMAPClient(
                    host=self._settings.imap_host,
                    port=self._settings.imap_port,
                    user=self._settings.imap_user,
                    password=self._settings.imap_password,
                )
                await imap.connect()
                emails = await imap.fetch_unseen()
                await imap.disconnect()

                if emails:
                    logger.info("IntakeAgent: %d new emails", len(emails))
                    for email_data in emails:
                        await self._process_email(email_data)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("IntakeAgent poll error: %s", exc)

            await asyncio.sleep(self._poll_interval)

    async def _process_email(self, email_data: dict[str, Any]) -> None:
        """Process a single email: dedup → LLM parse → classify → emit."""
        message_id = email_data.get("message_id", "")
        from_email = email_data.get("from_email", "")
        subject = email_data.get("subject", "")
        body = email_data.get("body", "")

        # 1. Dedup — check if we already processed this message_id
        if await self._is_processed(message_id):
            logger.debug("Skipping already-processed email: %s", message_id)
            return

        # Mark as processed immediately (idempotent)
        await self._mark_processed(message_id, email_data.get("date", ""))

        # 2. Parse with LLM
        parsed = await self._parse_email(from_email, subject, body)

        if not parsed:
            logger.warning("LLM returned empty parse for email from %s", from_email)
            return

        # 3. Extract common fields
        intent = parsed.get("intent", "other")
        confidence = float(parsed.get("confidence", 0.0))
        client_name = parsed.get("client_name")
        dog_name = parsed.get("dog_name")
        walk_date = parsed.get("walk_date")
        walk_slot = parsed.get("walk_slot")
        reason = parsed.get("reason", "")
        severity = parsed.get("severity", "medium")
        raw_message = f"From: {from_email}\nSubject: {subject}\n\n{body}"

        logger.info(
            "IntakeAgent: intent=%s confidence=%.2f from=%s dog=%s",
            intent, confidence, from_email, dog_name,
        )

        # 4. Check confidence threshold
        if confidence < self._confidence_threshold:
            await self.emit(
                HumanApprovalRequired(
                    gate_type="ambiguous_intent",
                    context={
                        "from_email": from_email,
                        "subject": subject,
                        "body_preview": body[:500],
                        "llm_parse": parsed,
                        "message_id": message_id,
                    },
                    options=["booking", "cancellation", "reschedule", "query", "complaint", "discard"],
                )
            )
            logger.info("IntakeAgent: low confidence (%.2f < %.2f) → human gate", confidence, self._confidence_threshold)
            return

        # 5. Check if client is known
        client_known = await self._is_known_client(from_email)
        if not client_known and intent != "query":
            await self.emit(
                HumanApprovalRequired(
                    gate_type="new_client",
                    context={
                        "from_email": from_email,
                        "client_name": client_name,
                        "dog_name": dog_name,
                        "intent": intent,
                        "walk_date": walk_date,
                        "walk_slot": walk_slot,
                        "subject": subject,
                        "body_preview": body[:500],
                        "message_id": message_id,
                    },
                    options=["approve_and_book", "approve_no_book", "reject"],
                )
            )
            logger.info("IntakeAgent: unknown client %s → new_client gate", from_email)
            return

        # 6. Emit the typed event
        await self._emit_intent(
            intent=intent,
            from_email=from_email,
            client_name=client_name,
            dog_name=dog_name,
            walk_date=walk_date,
            walk_slot=walk_slot,
            reason=reason,
            severity=severity,
            raw_message=raw_message,
            confidence=confidence,
        )

    async def _emit_intent(
        self,
        *,
        intent: str,
        from_email: str,
        client_name: str | None,
        dog_name: str | None,
        walk_date: str | None,
        walk_slot: str | None,
        reason: str,
        severity: str,
        raw_message: str,
        confidence: float,
    ) -> None:
        """Emit the appropriate typed event based on classified intent."""
        common = {
            "client_email": from_email,
            "client_name": client_name,
            "raw_message": raw_message,
        }

        if intent == "booking":
            await self.emit(BookingIntent(
                **common,
                dog_name=dog_name,
                walk_date=walk_date,
                walk_slot=walk_slot,
                confidence=confidence,
            ))
        elif intent == "cancellation":
            await self.emit(CancellationIntent(
                **common,
                walk_date=walk_date,
                reason=reason,
            ))
        elif intent == "reschedule":
            await self.emit(RescheduleIntent(
                **common,
                new_date=walk_date,
                new_slot=walk_slot,
            ))
        elif intent == "complaint":
            await self.emit(ComplaintIntent(
                **common,
                complaint_text=raw_message,
                severity=severity,
            ))
        elif intent == "query":
            await self.emit(QueryIntent(
                **common,
                query_text=raw_message,
            ))
        else:
            # Unknown intent → human gate
            await self.emit(HumanApprovalRequired(
                gate_type="unusual_request",
                context={
                    "from_email": from_email,
                    "intent": intent,
                    "raw_message": raw_message[:500],
                },
                options=["handle_manually", "discard"],
            ))

    # ── LLM Parsing ─────────────────────────────────────────

    async def _parse_email(self, from_email: str, subject: str, body: str) -> dict:
        """Send email to Ollama and parse the structured JSON response."""
        prompt = USER_PROMPT_TEMPLATE.format(
            from_email=from_email,
            subject=subject,
            body=body[:2000],  # truncate to avoid token overflow
        )
        try:
            result = await self._ollama.generate_json(
                prompt=prompt,
                system=SYSTEM_PROMPT,
                temperature=0.2,
            )
            return result
        except Exception as exc:
            logger.error("LLM parse failed: %s", exc)
            return {}

    # ── Dedup (SQLite) ──────────────────────────────────────

    async def _is_processed(self, message_id: str) -> bool:
        """Check if this email message_id has already been processed."""
        if not message_id:
            return False
        db = self._router.store.db
        rows = await db.execute_fetchall(
            "SELECT 1 FROM processed_emails WHERE message_id = ?",
            (message_id,),
        )
        return len(rows) > 0

    async def _mark_processed(self, message_id: str, received_at: str) -> None:
        """Record a message_id as processed."""
        if not message_id:
            return
        db = self._router.store.db
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO processed_emails (message_id, received_at, processed_at) VALUES (?, ?, ?)",
            (message_id, received_at or now, now),
        )
        await db.commit()

    # ── Client Lookup ──────────────────────────────────────

    async def _is_known_client(self, email: str) -> bool:
        """Check if this email belongs to a known client."""
        if not email:
            return False
        db = self._router.store.db
        rows = await db.execute_fetchall(
            "SELECT 1 FROM clients WHERE email = ? AND status = 'active'",
            (email,),
        )
        return len(rows) > 0
