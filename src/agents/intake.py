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
    ClarificationRequest,
    ComplaintIntent,
    DogInfoProvided,
    HumanApprovalRequired,
    OnboardingStarted,
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

CRITICAL — Extract these fields ONLY if they are concretely present in the email:
- client_name: The name from the email body/signature (e.g. "Sophie Lange" at the end
  of the email). Do NOT use the name from the From: header — that is the email account
  owner, not necessarily the client. Extract ONLY the name signed in the body.
- dog_name: The dog's name (must be explicitly mentioned, e.g. "Bello", not "my dog")
- walk_date: A SPECIFIC date. Accept formats:
    * ISO date: "2025-07-04"
    * Day of week with context: "this Friday", "next Monday"
    * Relative days: "tomorrow", "today" (resolve to YYYY-MM-DD based on current date)
    * Numeric date: "07.07", "7/5", "5.7.2025" — resolve to YYYY-MM-DD based on current date
    IMPORTANT: If the email contains an explicit numeric date (e.g. "07.07"), ALWAYS use
    that date. Do NOT override it with a relative interpretation like "next week".
    "next week Tuesday 07.07" means the date is 07.07 (July 7th), NOT next week + 7 days.
    If the date is vague (just "next week", "sometime soon", "maybe next week" with no
    specific day or number) → set to null
- walk_slot: A specific time slot ("11:30", "12:00", "12:30"). IMPORTANT: if the client
    says "any time", "any available slot", "whenever works", or doesn't mention a specific
    time → set walk_slot to null. This is NOT a missing field — the system will auto-assign
    the best slot. A booking with dog_name + walk_date + null walk_slot is STILL clear.
- reason: For cancellations/complaints, the stated reason
- severity: For complaints only — "low", "medium", or "high"

CLARITY ASSESSMENT — set the "clarity" field:
- "clear": All required fields for this intent are present and specific
- "needs_clarification": Required fields are missing or vague

Required fields by intent:
- booking: dog_name AND walk_date must both be present (walk_slot is optional)
- cancellation: dog_name AND (walk_date OR booking_ref) must be present
- reschedule: dog_name AND new_date must be present
- query: no required fields (always "clear")
- complaint: no required fields (always "clear")

IMPORTANT: missing_fields should ONLY list fields from the "Required fields" list above.
Never include optional fields (walk_slot, reason, severity) in missing_fields.

Respond as JSON only, no markdown:
{
  "intent": "booking|cancellation|reschedule|query|complaint|other",
  "clarity": "clear|needs_clarification",
  "missing_fields": ["field1", "field2"],
  "client_name": "string or null",
  "dog_name": "string or null",
  "walk_date": "YYYY-MM-DD or null",
  "walk_slot": "HH:MM or null",
  "reason": "string or null",
  "severity": "low|medium|high or null",
  "summary": "one-sentence summary of the email"
}"""

USER_PROMPT_TEMPLATE = """Parse this email for the dog-walking business.

Today's date: {today}
Current day of week: {dow}

From: {from_email}
Subject: {subject}

Body:
{body}

Respond as JSON only."""


ONBOARDING_SYSTEM_PROMPT = """You are an email parser for a dog-walking business called Walking Hounds.
A new client is replying to our onboarding questionnaire with information about their dog.
Extract the following fields from their reply:

- client_name: The sender's name
- dog_name: The dog's name (REQUIRED)
- breed: Dog breed (e.g. "Labrador", "mixed breed") (REQUIRED)
- age_months: Age in months. If given in years, multiply by 12 (REQUIRED)
- sex: "male" or "female" (REQUIRED)
- castrated: "neutered" (male), "spayed" (female), or "intact" if not fixed
- temperament: Personality description (calm, energetic, anxious, friendly, etc.)
- special_needs: Any special needs mentioned

Respond as JSON only, no markdown:
{
  "client_name": "string or null",
  "dog_name": "string or null",
  "breed": "string or null",
  "age_months": "number or null",
  "sex": "male|female|null",
  "castrated": "neutered|spayed|intact|null",
  "temperament": "string or null",
  "special_needs": "string or null",
  "summary": "one-sentence summary"
}"""


ONBOARDING_USER_TEMPLATE = """Parse this reply from a new client providing their dog's information.

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
                try:
                    await imap.connect()
                    emails = await imap.fetch_all(folder=self._settings.imap_folder, limit=10)
                    if emails:
                        logger.info("IntakeAgent: %d new emails", len(emails))
                        for email_data in emails:
                            await self._process_email(email_data)
                    else:
                        logger.debug("IntakeAgent: no new emails in '%s'", self._settings.imap_folder)
                finally:
                    await imap.disconnect()

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("IntakeAgent poll error: %s (%s)", exc, type(exc).__name__)

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
        clarity = parsed.get("clarity", "needs_clarification")
        missing_fields = parsed.get("missing_fields", [])
        client_name = parsed.get("client_name")
        dog_name = parsed.get("dog_name")
        walk_date = self._resolve_date(parsed.get("walk_date"))
        walk_slot = parsed.get("walk_slot")
        reason = parsed.get("reason", "")
        severity = parsed.get("severity", "medium")
        raw_message = f"From: {from_email}\nSubject: {subject}\n\n{body}"

        # Safety net: if LLM flagged only optional (non-required) fields as missing,
        # force clarity to "clear". The system auto-assigns walk_slot, reason is
        # informational, and severity defaults. Required fields are:
        #   booking: dog_name, walk_date
        #   cancellation: dog_name, (walk_date OR booking_ref)
        #   reschedule: dog_name, new_date
        #   query/complaint: none
        _required_by_intent: dict[str, set[str]] = {
            "booking": {"dog_name", "walk_date"},
            "cancellation": {"dog_name", "walk_date", "booking_ref"},
            "reschedule": {"dog_name", "new_date"},
            "query": set(),
            "complaint": set(),
        }
        required = _required_by_intent.get(intent, set())
        actual_missing = [f for f in missing_fields if f in required]
        if clarity == "needs_clarification" and not actual_missing and missing_fields:
            logger.info(
                "IntakeAgent: LLM flagged only optional fields (%s) as missing — "
                "overriding clarity to 'clear'", missing_fields,
            )
            clarity = "clear"
            missing_fields = []
        elif clarity == "needs_clarification" and actual_missing:
            missing_fields = actual_missing  # strip optional fields from the list

        logger.info(
            "IntakeAgent: intent=%s clarity=%s from=%s dog=%s",
            intent, clarity, from_email, dog_name,
        )

        # 3a. Onboarding detection: if this sender has an active onboarding session,
        # re-parse with dog-info prompt and emit DogInfoProvided instead.
        if await self._has_active_onboarding(from_email):
            onboarding_parsed = await self._parse_onboarding_reply(from_email, subject, body)
            if onboarding_parsed and onboarding_parsed.get("dog_name"):
                await self.emit(DogInfoProvided(
                    client_email=from_email,
                    client_name=onboarding_parsed.get("client_name") or client_name,
                    dog_name=onboarding_parsed["dog_name"],
                    breed=onboarding_parsed.get("breed", ""),
                    age_months=onboarding_parsed.get("age_months"),
                    sex=onboarding_parsed.get("sex", ""),
                    castrated=onboarding_parsed.get("castrated", ""),
                    temperament=onboarding_parsed.get("temperament", ""),
                    special_needs=onboarding_parsed.get("special_needs", ""),
                    raw_message=raw_message,
                ))
                logger.info(
                    "IntakeAgent: onboarding reply from %s (dog=%s) → DogInfoProvided",
                    from_email, onboarding_parsed["dog_name"],
                )
                return
            logger.debug("IntakeAgent: onboarding reply from %s had no dog_name, treating normally", from_email)

        # 4. Check clarity — if fields are missing/vague, request clarification
        if clarity == "needs_clarification" and missing_fields:
            await self.emit(
                ClarificationRequest(
                    client_email=from_email,
                    client_name=client_name,
                    intent=intent,
                    missing_fields=missing_fields,
                    original_message=raw_message,
                    suggested_clarification=self._build_clarification_text(intent, missing_fields),
                )
            )
            logger.info(
                "IntakeAgent: unclear email (missing %s) → clarification request",
                missing_fields,
            )
            return

        # 5. Check if client is known
        if self._settings.intake_demo_mode:
            # Demo mode: match by client name extracted from body, not email
            client_email = await self._find_client_by_name(client_name)
            if client_email:
                from_email = client_email  # Use seed client's email for downstream agents
                client_known = True
            else:
                # In demo mode, unknown names pass through — no gate
                client_known = True
                logger.info("IntakeAgent: demo mode — unknown name '%s', passing through", client_name)
        else:
            client_known = await self._is_known_client(from_email)

        if not client_known and intent != "query":
            await self.emit(OnboardingStarted(
                client_email=from_email,
                client_name=client_name,
                original_intent=intent,
                raw_message=raw_message,
            ))
            logger.info("IntakeAgent: unknown client %s → OnboardingStarted", from_email)
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

    @staticmethod
    def _build_clarification_text(intent: str, missing_fields: list[str]) -> str:
        """Build a polite clarification email body for the missing fields."""
        field_descriptions = {
            "dog_name": "your dog's name",
            "walk_date": "the specific date you'd like the walk (e.g. this Friday, 2025-07-04)",
            "new_date": "the new date you'd like to move the walk to",
            "booking_ref": "your booking reference or the original walk date",
        }
        missing_text = ", ".join(
            field_descriptions.get(f, f) for f in missing_fields
        )
        return (
            f"Thank you for your email! "
            f"To process your {intent} request, could you please clarify: {missing_text}?"
        )

    async def _parse_email(self, from_email: str, subject: str, body: str) -> dict:
        """Send email to Ollama and parse the structured JSON response."""
        now = datetime.now(timezone.utc)
        prompt = USER_PROMPT_TEMPLATE.format(
            today=now.date().isoformat(),
            dow=now.strftime("%A"),
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

    async def _has_active_onboarding(self, email: str) -> bool:
        """Check if this sender has an active onboarding session."""
        if not email:
            return False
        db = self._router.store.db
        rows = await db.execute_fetchall(
            """SELECT 1 FROM onboarding_sessions
               WHERE email = ? AND status IN ('awaiting_info', 'pending_approval')
               LIMIT 1""",
            (email,),
        )
        return len(rows) > 0

    async def _parse_onboarding_reply(self, from_email: str, subject: str, body: str) -> dict:
        """Parse a reply email as dog-info using the onboarding-specific prompt."""
        prompt = ONBOARDING_USER_TEMPLATE.format(
            from_email=from_email,
            subject=subject,
            body=body[:2000],
        )
        try:
            result = await self._ollama.generate_json(
                prompt=prompt,
                system=ONBOARDING_SYSTEM_PROMPT,
                temperature=0.2,
            )
            return result
        except Exception as exc:
            logger.error("IntakeAgent: onboarding parse failed: %s", exc)
            return {}

    @staticmethod
    def _resolve_date(date_str: str | None) -> str | None:
        """Resolve relative dates from LLM output to ISO format.
        
        Handles: 'this Monday', 'next Friday', 'tomorrow', 'today',
        ISO dates (passed through), European numeric dates (DD.MM or DD.MM.YYYY),
        and null/vague dates.
        """
        if not date_str:
            return None

        from datetime import date, timedelta
        import re

        today = date.today()
        date_lower = date_str.strip().lower()

        # Already ISO format? Pass through
        try:
            date.fromisoformat(date_str)
            return date_str
        except (ValueError, TypeError):
            pass

        # European numeric date: DD.MM, DD.MM.YY, or DD.MM.YYYY
        # e.g. "07.07" → 2026-07-07, "5.7.2025" → 2025-07-05
        euro_match = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$", date_str.strip())
        if euro_match:
            day = int(euro_match.group(1))
            month = int(euro_match.group(2))
            year_str = euro_match.group(3)
            if year_str:
                year = int(year_str)
                if year < 100:
                    year += 2000
            else:
                year = today.year
                # If the date has already passed this year, assume next year
                try:
                    if date(year, month, day) < today:
                        year += 1
                except ValueError:
                    return None
            try:
                resolved = date(year, month, day)
                return resolved.isoformat()
            except ValueError:
                return None

        # Slash dates: MM/DD or MM/DD/YYYY (US format)
        slash_match = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", date_str.strip())
        if slash_match:
            month = int(slash_match.group(1))
            day = int(slash_match.group(2))
            year_str = slash_match.group(3)
            if year_str:
                year = int(year_str)
                if year < 100:
                    year += 2000
            else:
                year = today.year
                try:
                    if date(year, month, day) < today:
                        year += 1
                except ValueError:
                    return None
            try:
                resolved = date(year, month, day)
                return resolved.isoformat()
            except ValueError:
                return None

        # Relative days
        if date_lower == "today":
            return today.isoformat()
        if date_lower == "tomorrow":
            return (today + timedelta(days=1)).isoformat()
        if date_lower == "yesterday":
            return (today - timedelta(days=1)).isoformat()

        # 'this Monday', 'next Friday', etc.
        day_names = {
            "monday": 0, "tuesday": 1, "wednesday": 2,
            "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
        }
        import re
        match = re.match(r"(this|next)\s+(\w+)", date_lower)
        if match:
            prefix, day = match.group(1), match.group(2)
            if day in day_names:
                target_weekday = day_names[day]
                current_weekday = today.weekday()
                if prefix == "this":
                    delta = (target_weekday - current_weekday) % 7
                else:  # "next"
                    delta = (target_weekday - current_weekday) % 7 + 7
                resolved = today + timedelta(days=delta)
                return resolved.isoformat()

        # Vague — return None
        return None

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

    async def _find_client_by_name(self, name: str | None) -> str | None:
        """Demo mode: find a seed client's email by their name. Returns email or None."""
        if not name:
            return None
        db = self._router.store.db
        rows = await db.execute_fetchall(
            "SELECT email FROM clients WHERE name = ? AND status = 'active'",
            (name,),
        )
        return rows[0]["email"] if rows else None
