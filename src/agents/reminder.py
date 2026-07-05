"""Reminder Agent — time-based triggers and notifications.

Subscribes to: (none — timer-driven)
Emits: ReminderDue, WalkCompleted

Runs a periodic timer loop that checks for:
- Walk reminders: REMINDER_HOURS_BEFORE_WALK (default 2h) before each scheduled walk
- Walker morning briefings: at 08:00 on business days
- Post-walk feedback requests: after walk slot ends (also marks walk as completed)

Not yet implemented (future work):
- Invoice overdue trigger (InvoicingAgent.check_overdue_invoices() exists, timer hook is a stub)
- Next-day schedule confirmation (20:00 reminder to clients)
- LoggerAgent daily summary journal entry (currently reactive only)

All timers are recomputed from target times on boot — survives restarts.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite

from ..router.event import (
    BaseEvent,
    ReminderDue,
    ScheduleConfirmed,
    WalkCompleted,
)
from ..router.router import EventRouter
from ..config import Settings, get_settings
from .base import BaseAgent

logger = logging.getLogger(__name__)


class ReminderAgent(BaseAgent):
    """Fires time-based triggers for walks, briefings, and feedback."""

    name = "ReminderAgent"

    def __init__(self, router: EventRouter, settings: Settings | None = None):
        super().__init__(router)
        self._settings = settings or get_settings()
        self._db: aiosqlite.Connection | None = None
        self._timer_task: asyncio.Task | None = None
        self._poll_interval = self._settings.reminder_poll_interval_sec
        self._reminder_hours = self._settings.reminder_hours_before_walk

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = self._router.store.db
        return self._db

    def subscribed_event_types(self) -> list[str]:
        # We don't need to subscribe to events — we poll the database.
        # But we do track ScheduleConfirmed to know about new walks.
        return []

    async def on_start(self) -> None:
        self._timer_task = asyncio.create_task(self._timer_loop(), name="reminder-timer")
        logger.info(
            "ReminderAgent started — polling every %ds, reminders %dh before walk",
            self._poll_interval, self._reminder_hours,
        )

    async def on_stop(self) -> None:
        if self._timer_task:
            self._timer_task.cancel()
            try:
                await asyncio.wait_for(self._timer_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        logger.info("ReminderAgent stopped")

    async def handle(self, event: BaseEvent) -> None:
        # Reminder doesn't receive events — it's timer-driven
        pass

    # ── Timer Loop ──────────────────────────────────────────

    async def _timer_loop(self) -> None:
        """Main loop — checks every poll_interval for triggers."""
        while True:
            try:
                await self._check_walk_reminders()
                await self._check_walk_completions()
                await self._check_morning_briefings()
                await self._check_invoice_overdue()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("ReminderAgent timer error: %s", exc)

            await asyncio.sleep(self._poll_interval)

    # ── Walk Reminders ──────────────────────────────────────

    async def _check_walk_reminders(self) -> None:
        """Send walk reminders REMINDER_HOURS before walk time."""
        now = _now()
        reminder_window = timedelta(hours=self._reminder_hours)
        # Check window: walks starting within the next reminder window
        # that haven't been reminded yet

        # We track reminders via the messages table — if a walk_reminder
        # message was already sent for this walk, skip it.
        rows = await self.db.execute_fetchall(
            """SELECT w.*, c.email as client_email, c.name as client_name,
                      d.name as dog_name, wl.name as walker_name
               FROM walks w
               JOIN clients c ON w.client_id = c.id
               JOIN dogs d ON w.dog_id = d.id
               JOIN walkers wl ON w.walker_id = wl.id
               WHERE w.status = 'scheduled'
               AND w.date = ?""",
            (now.date().isoformat(),),
        )

        for row in rows:
            walk = dict(row)
            walk_dt = self._parse_walk_datetime(walk["date"], walk["slot"])
            if not walk_dt:
                continue

            time_until = walk_dt - now

            # If walk is within the reminder window (e.g. within 2 hours)
            if timedelta(0) < time_until <= reminder_window:
                # Check if reminder already sent
                if await self._is_reminder_sent(walk["id"], "walk_reminder"):
                    continue

                await self.emit(ReminderDue(
                    reminder_type="walk_reminder",
                    target_email=walk["client_email"],
                    booking_id=walk["id"],
                    walk_date=walk["date"],
                    walk_slot=walk["slot"],
                ))

                await self._mark_reminder_sent(walk["id"], "walk_reminder")

                logger.info(
                    "ReminderAgent: walk reminder for %s (%s at %s)",
                    walk["dog_name"], walk["date"], walk["slot"],
                )

    async def _check_walk_completions(self) -> None:
        """Mark walks as completed when their slot time has passed.

        Emits WalkCompleted and then a feedback request."""
        now = _now()

        rows = await self.db.execute_fetchall(
            """SELECT w.*, c.email as client_email, c.name as client_name,
                      d.name as dog_name, wl.name as walker_name
               FROM walks w
               JOIN clients c ON w.client_id = c.id
               JOIN dogs d ON w.dog_id = d.id
               JOIN walkers wl ON w.walker_id = wl.id
               WHERE w.status = 'scheduled'
               AND w.date = ?""",
            (now.date().isoformat(),),
        )

        for row in rows:
            walk = dict(row)
            walk_dt = self._parse_walk_datetime(walk["date"], walk["slot"])
            if not walk_dt:
                continue

            # Walk duration is 60 minutes by default
            walk_end = walk_dt + timedelta(minutes=walk.get("duration", 60))

            if now > walk_end:
                # Walk time has passed — mark as completed
                await self.db.execute(
                    "UPDATE walks SET status = 'completed' WHERE id = ?",
                    (walk["id"],),
                )
                await self.db.commit()

                await self.emit(WalkCompleted(
                    booking_id=walk["id"],
                    walker_name=walk["walker_name"],
                    dog_name=walk["dog_name"],
                    duration_min=walk.get("duration", 60),
                ))

                # Send feedback request
                if not await self._is_reminder_sent(walk["id"], "feedback"):
                    await self.emit(ReminderDue(
                        reminder_type="feedback",
                        target_email=walk["client_email"],
                        booking_id=walk["id"],
                        walk_date=walk["date"],
                    ))
                    await self._mark_reminder_sent(walk["id"], "feedback")

                logger.info(
                    "ReminderAgent: walk %s completed (%s)",
                    walk["id"], walk["dog_name"],
                )

    # ── Morning Briefings ───────────────────────────────────

    async def _check_morning_briefings(self) -> None:
        """Send walker morning briefings at 08:00 on business days."""
        now = _now()

        # Only at 08:00 ± poll_interval/2
        if now.hour != 8:
            return

        if now.strftime("%a").lower() not in self._settings.business_day_list:
            return

        # Check if briefing already sent today for each walker
        rows = await self.db.execute_fetchall(
            """SELECT DISTINCT wl.id as walker_id, wl.name as walker_name, wl.email
               FROM walkers wl
               WHERE wl.active = 1"""
        )

        for row in rows:
            walker = dict(row)
            if not walker.get("email"):
                continue

            briefing_key = f"briefing_{walker['walker_id']}_{now.date().isoformat()}"

            if await self._is_reminder_sent(briefing_key, "walker_briefing"):
                continue

            # Get today's walks for this walker
            walk_rows = await self.db.execute_fetchall(
                """SELECT w.*, d.name as dog_name, d.breed, d.special_needs
                   FROM walks w
                   JOIN dogs d ON w.dog_id = d.id
                   WHERE w.walker_id = ? AND w.date = ? AND w.status = 'scheduled'
                   ORDER BY w.slot""",
                (walker["walker_id"], now.date().isoformat()),
            )

            if not walk_rows:
                continue  # No walks today, no briefing needed

            walks = [dict(r) for r in walk_rows]

            await self.emit(ReminderDue(
                reminder_type="walker_briefing",
                target_email=walker["email"],
                booking_id=None,
                walk_date=now.date().isoformat(),
            ))

            await self._mark_reminder_sent(briefing_key, "walker_briefing")

            logger.info(
                "ReminderAgent: morning briefing for %s (%d walks)",
                walker["walker_name"], len(walks),
            )

    # ── Invoice Overdue Check ───────────────────────────────

    async def _check_invoice_overdue(self) -> None:
        """Delegate to InvoicingAgent if it's registered on the router."""
        # We call check_overdue_invoices via a direct method on the DB
        # rather than inter-agent communication.
        # The InvoicingAgent.check_overdue_invoices() method reads from
        # the same SQLite DB and emits the appropriate events.
        # Here we just trigger the check periodically.
        # In a real system, this would be done by the InvoicingAgent's
        # own timer or via a cron-like trigger.
        pass  # InvoicingAgent has its own check; this is a hook for future use

    # ── Reminder Tracking (dedup via messages table) ────────

    async def _is_reminder_sent(self, booking_id: str, reminder_type: str) -> bool:
        """Check if a reminder was already sent (dedup via messages table)."""
        rows = await self.db.execute_fetchall(
            """SELECT 1 FROM messages
               WHERE subject LIKE ? AND body LIKE ? AND sent_at LIKE ?""",
            (
                f"%{reminder_type}%",
                f"%{booking_id}%",
                f"{_now().date().isoformat()}%",
            ),
        )
        return len(rows) > 0

    async def _mark_reminder_sent(self, booking_id: str, reminder_type: str) -> None:
        """Record that a reminder was sent (for dedup)."""
        now_dt = _now()
        now_iso = now_dt.isoformat()
        today = now_dt.date().isoformat()
        msg_id = f"reminder_{reminder_type}_{booking_id}_{today}"

        await self.db.execute(
            """INSERT OR IGNORE INTO messages (id, direction, channel, subject, body, sent_at, status)
               VALUES (?, 'outbound', 'reminder', ?, ?, ?, 'sent')""",
            (msg_id, f"{reminder_type}", f"booking_id={booking_id}", now_iso),
        )
        await self.db.commit()

    # ── Helpers ─────────────────────────────────────────────

    @staticmethod
    def _parse_walk_datetime(date_str: str | None, slot_str: str | None) -> datetime | None:
        """Parse a walk date + slot into a datetime (UTC)."""
        if not date_str or not slot_str:
            return None
        try:
            parts = slot_str.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            return datetime.fromisoformat(date_str).replace(
                hour=hour, minute=minute, tzinfo=timezone.utc
            )
        except (ValueError, TypeError, IndexError):
            return None


def _now() -> datetime:
    return datetime.now(timezone.utc)
