"""Scheduling Agent — manages the walk calendar, walker assignments,
group composition, conflict detection, and cancellations.

Subscribes to: BookingIntent, CancellationIntent, RescheduleIntent, HumanApproved
Emits: ScheduleConfirmed, ScheduleConflict, CancellationConfirmed, ScheduleUpdated

Key business rules:
- Walk slots: 11:30, 12:00, 12:30 (staggered)
- Max 4 dogs per group, max 3 groups per day (min 2)
- Puppies (4-10 months) grouped together in a puppy group
- Business days: Mon-Fri only
- Cancellation > 24h: full refund, < 24h: 50% charge
- Auto-assign walker with fewest walks that day
- Conflicts escalate to human when unresolvable
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import Any

import aiosqlite

from ..router.event import (
    BaseEvent,
    BookingIntent,
    CancellationIntent,
    CancellationConfirmed,
    HumanApproved,
    RescheduleIntent,
    ScheduleConflict,
    ScheduleConfirmed,
    ScheduleUpdated,
)
from ..router.router import EventRouter
from ..config import Settings, get_settings
from .base import BaseAgent

logger = logging.getLogger(__name__)


def _uuid() -> str:
    return uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SchedulingAgent(BaseAgent):
    """Manages the walk calendar, walker assignment, and group composition."""

    name = "SchedulingAgent"

    def __init__(self, router: EventRouter, settings: Settings | None = None):
        super().__init__(router)
        self._settings = settings or get_settings()
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = self._router.store.db
        return self._db

    def subscribed_event_types(self) -> list[str]:
        return [
            "BookingIntent",
            "CancellationIntent",
            "RescheduleIntent",
            "HumanApproved",
        ]

    async def handle(self, event: BaseEvent) -> None:
        if isinstance(event, BookingIntent):
            await self._handle_booking(event)
        elif isinstance(event, CancellationIntent):
            await self._handle_cancellation(event)
        elif isinstance(event, RescheduleIntent):
            await self._handle_reschedule(event)
        elif isinstance(event, HumanApproved):
            await self._handle_human_approved(event)

    # ── Booking ─────────────────────────────────────────────

    async def _handle_booking(self, event: BookingIntent) -> None:
        """Process a booking intent: find or create a walk slot."""
        walk_date = event.walk_date
        walk_slot = event.walk_slot

        if not walk_date:
            # No date specified → conflict (can't schedule without date)
            await self.emit(ScheduleConflict(
                conflict_details="No walk date specified in booking request",
                alternatives=[],
                original_intent_event_id=event.id,
            ))
            return

        # Reject dates in the past
        today = datetime.now(timezone.utc).date()
        try:
            walk_date_dt = datetime.fromisoformat(walk_date).date()
        except ValueError:
            await self.emit(ScheduleConflict(
                conflict_details=f"Invalid walk date format: '{walk_date}'",
                alternatives=[],
                original_intent_event_id=event.id,
            ))
            return
        if walk_date_dt < today:
            await self.emit(ScheduleConflict(
                conflict_details=f"{walk_date} is in the past (today is {today.isoformat()})",
                alternatives=self._suggest_next_business_days(today.isoformat(), 3),
                original_intent_event_id=event.id,
            ))
            return

        # Check if business day
        if not self._is_business_day(walk_date):
            await self.emit(ScheduleConflict(
                conflict_details=f"{walk_date} is not a business day (Mon-Fri only)",
                alternatives=self._suggest_next_business_days(walk_date, 3),
                original_intent_event_id=event.id,
            ))
            return

        # Find the dog in the database
        dog = await self._find_dog(event.client_email, event.dog_name)
        if not dog:
            await self.emit(ScheduleConflict(
                conflict_details=f"Dog '{event.dog_name}' not found for client '{event.client_email}'",
                alternatives=[],
                original_intent_event_id=event.id,
            ))
            return

        # Determine the walk slot
        if not walk_slot:
            walk_slot = await self._find_available_slot(walk_date, dog)
            if not walk_slot:
                await self.emit(ScheduleConflict(
                    conflict_details=f"No available slots on {walk_date}",
                    alternatives=self._suggest_next_business_days(walk_date, 3),
                    original_intent_event_id=event.id,
                ))
                return

        # Check for duplicate booking (same dog, same date, same slot)
        existing = await self._find_existing_walk(dog["id"], walk_date)
        if existing:
            if existing["status"] == "cancelled":
                # Reactivate cancelled walk
                await self._reactivate_walk(existing["id"], walk_slot)
            else:
                await self.emit(ScheduleConflict(
                    conflict_details=f"{dog['name']} already has a walk on {walk_date}",
                    alternatives=[],
                    original_intent_event_id=event.id,
                ))
                return

        # Find or create a group for this dog
        group = await self._find_or_create_group(walk_date, walk_slot, dog)

        if not group:
            # Slot is full — try another slot
            alt_slot = await self._find_available_slot(walk_date, dog, exclude=walk_slot)
            if alt_slot:
                walk_slot = alt_slot
                group = await self._find_or_create_group(walk_date, walk_slot, dog)
            else:
                await self.emit(ScheduleConflict(
                    conflict_details=f"All slots full on {walk_date}",
                    alternatives=self._suggest_next_business_days(walk_date, 3),
                    original_intent_event_id=event.id,
                ))
                return

        # Use the group's walker if available, otherwise find a new one
        walker = None
        if group.get("walker_id"):
            rows = await self.db.execute_fetchall(
                "SELECT * FROM walkers WHERE id = ? AND active = 1",
                (group["walker_id"],),
            )
            if rows:
                walker = dict(rows[0])

        if not walker:
            walker = await self._find_available_walker(walk_date, walk_slot)
            if not walker:
                await self.emit(ScheduleConflict(
                    conflict_details=f"No available walker for {walk_date} {walk_slot}",
                    alternatives=self._suggest_next_business_days(walk_date, 3),
                    original_intent_event_id=event.id,
                ))
                return

        # Create the walk
        walk_id = _uuid()
        now = _now()
        await self.db.execute(
            """INSERT INTO walks (id, client_id, dog_id, walker_id, group_id, date, slot, duration, status, price_eur, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)""",
            (walk_id, dog["client_id"], dog["id"], walker["id"], group["id"],
             walk_date, walk_slot, self._settings.walk_duration_min, self._settings.walk_price_eur, now),
        )
        await self.db.commit()

        logger.info(
            "SchedulingAgent: booked %s (dog=%s, walker=%s, %s %s, group=%s)",
            walk_id, dog["name"], walker["name"], walk_date, walk_slot, group["id"],
        )

        await self.emit(ScheduleConfirmed(
            booking_id=walk_id,
            client_email=event.client_email,
            client_name=event.client_name or "",
            dog_name=dog["name"],
            walker_id=walker["id"],
            walker_name=walker["name"],
            walk_date=walk_date,
            walk_slot=walk_slot,
            group_id=group["id"],
        ))

    # ── Cancellation ─────────────────────────────────────────

    async def _handle_cancellation(self, event: CancellationIntent) -> None:
        """Cancel an existing walk, check for late cancellation fee."""
        dog = await self._find_dog(event.client_email, event.dog_name)

        if not dog:
            logger.warning("SchedulingAgent: dog not found for cancellation from %s", event.client_email)
            return

        walk = await self._find_existing_walk(dog["id"], event.walk_date)
        if not walk:
            logger.warning("SchedulingAgent: no walk found to cancel for %s on %s", dog["name"], event.walk_date)
            return

        # Check if late cancellation
        walk_datetime = self._parse_walk_datetime(walk["date"], walk["slot"])
        now = datetime.now(timezone.utc)
        hours_until_walk = (walk_datetime - now).total_seconds() / 3600 if walk_datetime else 999

        is_late = hours_until_walk < self._settings.late_cancel_hours
        refund_percent = 50 if is_late else 100

        # Mark walk as cancelled
        await self.db.execute(
            "UPDATE walks SET status='cancelled' WHERE id=?",
            (walk["id"],),
        )
        await self.db.commit()

        logger.info(
            "SchedulingAgent: cancelled walk %s (dog=%s, late=%s, refund=%d%%)",
            walk["id"], dog["name"], is_late, refund_percent,
        )

        await self.emit(CancellationConfirmed(
            booking_id=walk["id"],
            client_email=event.client_email,
            client_name=event.client_name or "",
            refund_percent=refund_percent,
            late_cancellation=is_late,
        ))

    # ── Reschedule ───────────────────────────────────────────

    async def _handle_reschedule(self, event: RescheduleIntent) -> None:
        """Reschedule a walk to a new date/slot."""
        dog = await self._find_dog(event.client_email, event.dog_name or "")

        if not dog:
            logger.warning("SchedulingAgent: dog not found for reschedule from %s", event.client_email)
            return

        walk = await self._find_existing_walk(dog["id"], event.original_date)
        if not walk:
            logger.warning("SchedulingAgent: no walk found to reschedule for %s", dog["name"])
            return

        new_date = event.new_date
        new_slot = event.new_slot or walk["slot"]

        if not new_date:
            logger.warning("SchedulingAgent: no new date for reschedule")
            return

        if not self._is_business_day(new_date):
            await self.emit(ScheduleConflict(
                conflict_details=f"{new_date} is not a business day",
                alternatives=self._suggest_next_business_days(new_date, 3),
                original_intent_event_id=event.id,
            ))
            return

        # Find walker for new slot
        walker = await self._find_available_walker(new_date, new_slot)
        if not walker:
            await self.emit(ScheduleConflict(
                conflict_details=f"No walker available for {new_date} {new_slot}",
                alternatives=self._suggest_next_business_days(new_date, 3),
                original_intent_event_id=event.id,
            ))
            return

        old_date = walk["date"]
        old_slot = walk["slot"]

        # Update the walk
        await self.db.execute(
            "UPDATE walks SET date=?, slot=?, walker_id=? WHERE id=?",
            (new_date, new_slot, walker["id"], walk["id"]),
        )
        await self.db.commit()

        logger.info(
            "SchedulingAgent: rescheduled walk %s (%s %s → %s %s)",
            walk["id"], old_date, old_slot, new_date, new_slot,
        )

        await self.emit(ScheduleUpdated(
            booking_id=walk["id"],
            old_date=old_date,
            old_slot=old_slot,
            new_date=new_date,
            new_slot=new_slot,
            walker_id=walker["id"],
            walker_name=walker["name"],
        ))

    # ── Human Approved ──────────────────────────────────────

    async def _handle_human_approved(self, event: HumanApproved) -> None:
        """Handle a human approval — currently for new client bookings.
        The client/dog should have been created by the dashboard.
        This re-processes the original booking intent."""
        # The dashboard creates the client + dog records when approving,
        # then the human can forward the original email or the dashboard
        # can re-emit the BookingIntent. For now, we just log.
        logger.info(
            "SchedulingAgent: human approved gate %s (decision=%s)",
            event.gate_id, event.decision,
        )

    # ── Walker Assignment ──────────────────────────────────

    async def _find_available_walker(self, walk_date: str, walk_slot: str) -> dict | None:
        """Find the walker with the fewest walks on this date who isn't already
        assigned to another group at the same slot or the immediately preceding slot.

        Business rule: each walk is 45 min + 15 min break = 60 min total commitment.
        With slots spaced 30 min apart, a walker booked at 11:30 cannot take 12:00
        (they're still walking/breaking) but can take 12:30.
        """
        # Compute the previous slot, if any, for back-to-back exclusion
        slots = self._settings.walk_slot_list
        prev_slot = None
        try:
            idx = slots.index(walk_slot)
            if idx > 0:
                prev_slot = slots[idx - 1]
        except ValueError:
            pass

        rows = await self.db.execute_fetchall(
            """SELECT w.id, w.name,
                      (SELECT COUNT(*) FROM walks w2
                       WHERE w2.walker_id = w.id AND w2.date = ? AND w2.status = 'scheduled') as walk_count
               FROM walkers w
               WHERE w.active = 1
               AND w.id NOT IN (
                   SELECT walker_id FROM walks
                   WHERE date = ? AND status = 'scheduled'
                   AND slot IN (?, ?)
               )
               ORDER BY walk_count ASC
               LIMIT 1""",
            (walk_date, walk_date, walk_slot, prev_slot or walk_slot),
        )
        return dict(rows[0]) if rows else None

    # ── Group Management ────────────────────────────────────

    async def _find_or_create_group(
        self, walk_date: str, walk_slot: str, dog: dict,
    ) -> dict | None:
        """Find an existing group with capacity, or create a new one.

        Grouping rules:
        - Puppies (4-10 months) go into a puppy group, never mixed with adults.
        - In-heat females: only groups with no intact (non-neutered) males.
        - Intact males: only groups with no in-heat females.
        - These two rules naturally steer in-heat females and intact males
          into separate groups without requiring a dedicated group type.
        """
        is_puppy = self._is_puppy(dog)
        group_type = "puppy" if is_puppy else "standard"

        # Build a "conflict exclusion" clause for heat/intact rules.
        # When the arriving dog is an in-heat female, exclude groups that
        # already contain intact males. When it's an intact male, exclude
        # groups that already contain in-heat females.
        conflict_exclusion = ""

        if self._is_in_heat(dog):
            # In-heat female → exclude groups with intact males
            conflict_exclusion = """
               AND g.id NOT IN (
                   SELECT w.group_id FROM walks w
                   JOIN dogs d ON w.dog_id = d.id
                   WHERE w.group_id = g.id
                     AND w.status = 'scheduled'
                     AND d.sex = 'male'
                     AND d.castrated = 'intact'
               )"""
        elif self._is_intact_male(dog):
            # Intact male → exclude groups with in-heat females
            conflict_exclusion = """
               AND g.id NOT IN (
                   SELECT w.group_id FROM walks w
                   JOIN dogs d ON w.dog_id = d.id
                   WHERE w.group_id = g.id
                     AND w.status = 'scheduled'
                     AND d.sex = 'female'
                     AND d.in_heat = 1
               )"""

        # Try to find an existing group with capacity
        rows = await self.db.execute_fetchall(
            f"""SELECT g.id, g.name, g.walker_id, g.max_dogs, g.group_type,
                      (SELECT COUNT(*) FROM walks w
                       WHERE w.group_id = g.id AND w.status = 'scheduled') as dog_count
               FROM walk_groups g
               WHERE g.date = ? AND g.slot = ? AND g.group_type = ?
               AND (SELECT COUNT(*) FROM walks w
                    WHERE w.group_id = g.id AND w.status = 'scheduled') < g.max_dogs{conflict_exclusion}
               LIMIT 1""",
            (walk_date, walk_slot, group_type),
        )

        if rows:
            return dict(rows[0])

        # Check max groups per day
        existing_groups = await self.db.execute_fetchall(
            """SELECT COUNT(DISTINCT group_id) as cnt FROM walks
               WHERE date = ? AND status = 'scheduled' AND group_id IS NOT NULL""",
            (walk_date,),
        )
        if existing_groups and existing_groups[0]["cnt"] >= self._settings.max_groups_per_day:
            return None  # No room for more groups

        # Business rule: puppies and adults must be in DIFFERENT time slots.
        # Don't create a new group at this slot if the opposite group type
        # already has an active group with scheduled walks here.
        conflicting_type = "standard" if is_puppy else "puppy"
        slot_conflict = await self.db.execute_fetchall(
            """SELECT 1 FROM walk_groups g
               WHERE g.date = ? AND g.slot = ? AND g.group_type = ?
               AND EXISTS (
                   SELECT 1 FROM walks w
                   WHERE w.group_id = g.id AND w.status = 'scheduled'
               ) LIMIT 1""",
            (walk_date, walk_slot, conflicting_type),
        )
        if slot_conflict:
            # This slot already has the opposite group type — don't mix
            return None

        # Find a walker who doesn't have a group at this slot
        walker = await self._find_available_walker(walk_date, walk_slot)
        if not walker:
            return None

        # Create new group
        group_id = _uuid()
        group_name = f"{'Puppy' if is_puppy else 'Group'}-{walk_slot}"
        now = _now()

        await self.db.execute(
            """INSERT INTO walk_groups (id, name, walker_id, date, slot, max_dogs, group_type)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (group_id, group_name, walker["id"], walk_date, walk_slot,
             self._settings.max_dogs_per_group, group_type),
        )
        await self.db.commit()

        return {
            "id": group_id,
            "name": group_name,
            "walker_id": walker["id"],
            "max_dogs": self._settings.max_dogs_per_group,
            "group_type": group_type,
        }

    async def _find_available_slot(
        self, walk_date: str, dog: dict, exclude: str | None = None,
    ) -> str | None:
        """Find the first available slot for this dog on this date."""
        for slot in self._settings.walk_slot_list:
            if exclude and slot == exclude:
                continue
            group = await self._find_or_create_group(walk_date, slot, dog)
            if group:
                return slot
        return None

    # ── Lookup Helpers ──────────────────────────────────────

    async def _find_dog(self, client_email: str, dog_name: str | None) -> dict | None:
        """Find a dog by client email and dog name."""
        if not dog_name:
            return None
        rows = await self.db.execute_fetchall(
            """SELECT d.*, c.email as client_email, c.name as client_name, c.id as client_id
               FROM dogs d
               JOIN clients c ON d.client_id = c.id
               WHERE c.email = ? AND d.name = ?""",
            (client_email, dog_name),
        )
        return dict(rows[0]) if rows else None

    async def _find_existing_walk(self, dog_id: str, walk_date: str | None) -> dict | None:
        """Find an existing walk for this dog on this date."""
        if not walk_date:
            return None
        rows = await self.db.execute_fetchall(
            "SELECT * FROM walks WHERE dog_id = ? AND date = ? ORDER BY created_at DESC LIMIT 1",
            (dog_id, walk_date),
        )
        return dict(rows[0]) if rows else None

    async def _reactivate_walk(self, walk_id: str, walk_slot: str) -> None:
        """Reactivate a previously cancelled walk."""
        await self.db.execute(
            "UPDATE walks SET status='scheduled', slot=? WHERE id=?",
            (walk_slot, walk_id),
        )
        await self.db.commit()

    # ── Business Logic Helpers ──────────────────────────────

    @staticmethod
    def _is_puppy(dog: dict) -> bool:
        """Check if a dog is a puppy (4-10 months old)."""
        age = dog.get("age_months", 0)
        return 4 <= age <= 10

    @staticmethod
    def _is_in_heat(dog: dict) -> bool:
        """Check if a female dog is currently in heat (läufig)."""
        return dog.get("sex") == "female" and dog.get("in_heat", 0) == 1

    @staticmethod
    def _is_intact_male(dog: dict) -> bool:
        """Check if a male dog is intact (not neutered/castrated)."""
        return dog.get("sex") == "male" and dog.get("castrated", "") == "intact"

    def _is_business_day(self, date_str: str) -> bool:
        """Check if a date falls on a business day (Mon-Fri)."""
        try:
            dt = datetime.fromisoformat(date_str)
            return dt.strftime("%a").lower() in self._settings.business_day_list
        except (ValueError, TypeError):
            return True  # If we can't parse, assume yes

    def _suggest_next_business_days(self, from_date: str, count: int) -> list[dict]:
        """Suggest the next N business days after from_date."""
        try:
            dt = datetime.fromisoformat(from_date)
        except (ValueError, TypeError):
            dt = datetime.now(timezone.utc)

        suggestions = []
        current = dt + timedelta(days=1)
        while len(suggestions) < count:
            if current.strftime("%a").lower() in self._settings.business_day_list:
                suggestions.append({
                    "date": current.date().isoformat(),
                    "day": current.strftime("%A"),
                })
            current += timedelta(days=1)
        return suggestions

    @staticmethod
    def _parse_walk_datetime(date_str: str, slot_str: str) -> datetime | None:
        """Parse a walk date + slot into a datetime object."""
        try:
            time_part = slot_str.split(":")
            hour = int(time_part[0])
            minute = int(time_part[1]) if len(time_part) > 1 else 0
            dt = datetime.fromisoformat(date_str).replace(
                hour=hour, minute=minute, tzinfo=timezone.utc
            )
            return dt
        except (ValueError, TypeError, IndexError):
            return None
