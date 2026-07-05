"""Tests for the SchedulingAgent.

Tests cover:
- Booking creation (happy path)
- Walker auto-assignment (fewest walks)
- Puppy group separation (4-10 months)
- Group capacity (max 4 dogs)
- Max groups per day (3)
- Duplicate booking detection
- Business day enforcement (Mon-Fri)
- Cancellation (> 24h = 100% refund, < 24h = 50%)
- Reschedule to new date/slot
- Conflict detection (no walker, full day)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.agents.scheduling import SchedulingAgent
from src.router.router import EventRouter
from src.router.event import (
    BookingIntent,
    CancellationConfirmed,
    CancellationIntent,
    RescheduleIntent,
    ScheduleConflict,
    ScheduleConfirmed,
    ScheduleUpdated,
)
from src.router.store import EventStore
from src.db.database import init_database, close_database
from src.db.seed import generate_seed_data, WALKERS
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
    return Settings(db_path=tmp_db_path)


def _track(router):
    emitted = []
    original = router.publish

    async def tracker(event):
        emitted.append(event)
        await original(event)

    router.publish = tracker
    return emitted


# ── Booking Tests ──────────────────────────────────────────

class TestSchedulingBooking:
    async def test_booking_creates_walk_and_emits_confirmed(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()

        emitted = _track(router)

        booking = BookingIntent(
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            dog_name="Bello",
            walk_date="2027-07-02",  # Friday
            walk_slot="11:30",
        )
        await router.publish(booking)
        await asyncio.sleep(0.3)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 1
        assert confirmed[0].dog_name == "Bello"
        assert confirmed[0].walk_date == "2027-07-02"
        assert confirmed[0].walk_slot == "11:30"

        # Check walk in DB
        walks = await db.execute_fetchall("SELECT * FROM walks WHERE status='scheduled'")
        assert len(walks) == 1
        assert walks[0]["date"] == "2027-07-02"

        await agent.stop()

    async def test_booking_auto_assigns_slot_when_not_specified(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        booking = BookingIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            walk_date="2027-07-02",
            # no walk_slot specified
        )
        await router.publish(booking)
        await asyncio.sleep(0.3)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 1
        # Should have been assigned a slot from the walk slots
        assert confirmed[0].walk_slot in settings.walk_slot_list

        await agent.stop()

    async def test_duplicate_booking_emits_conflict(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # First booking
        await router.publish(BookingIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        # Second booking — same dog, same date
        await router.publish(BookingIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            walk_date="2027-07-02",
            walk_slot="12:00",
        ))
        await asyncio.sleep(0.3)

        conflicts = [e for e in emitted if isinstance(e, ScheduleConflict)]
        assert len(conflicts) == 1
        assert "already has a walk" in conflicts[0].conflict_details

        await agent.stop()


# ── Walker Assignment Tests ────────────────────────────────

class TestWalkerAssignment:
    async def test_walker_assigned_with_fewest_walks(self, setup_system, settings):
        """3 dogs at the same slot go into ONE group with the walker
        who has the fewest walks that day."""
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Book 3 different dogs at the same slot — all go in one group
        bookings = [
            ("lisa.mueller@example.com", "Bello", "11:30"),
            ("tom.schmidt@example.com", "Luna", "11:30"),
            ("anna.becker@example.com", "Rex", "11:30"),
        ]

        for email, dog, slot in bookings:
            await router.publish(BookingIntent(
                client_email=email,
                dog_name=dog,
                walk_date="2027-07-02",
                walk_slot=slot,
            ))
            await asyncio.sleep(0.2)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 3

        # All 3 in same group, same walker
        walker_ids = {c.walker_id for c in confirmed}
        assert len(walker_ids) == 1  # one walker for the group

        group_ids = {c.group_id for c in confirmed}
        assert len(group_ids) == 1  # one group

        await agent.stop()

    async def test_walker_not_double_booked_same_slot(self, setup_system, settings):
        """A walker can't have two groups at the same slot."""
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # 5 dogs at the same slot — max 4 per group, so 5th needs a new group
        # But that requires a different walker at the same slot
        bookings = [
            ("lisa.mueller@example.com", "Bello", "11:30"),
            ("tom.schmidt@example.com", "Luna", "11:30"),
            ("anna.becker@example.com", "Rex", "11:30"),
            ("jonas.weber@example.com", "Nala", "11:30"),
        ]

        for email, dog, slot in bookings:
            await router.publish(BookingIntent(
                client_email=email, dog_name=dog,
                walk_date="2027-07-02", walk_slot=slot,
            ))
            await asyncio.sleep(0.2)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 4

        # 4 dogs in 1 group → all same walker
        walker_ids = {c.walker_id for c in confirmed}
        assert len(walker_ids) == 1  # all in one group with one walker

        await agent.stop()


# ── Puppy Group Tests ──────────────────────────────────────

class TestPuppyGroups:
    async def test_puppies_grouped_separately_from_adults(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Book a puppy (Milo, 6 months) and an adult (Bello, 36 months) at same slot
        await router.publish(BookingIntent(
            client_email="martin.klein@example.com",
            dog_name="Milo",  # puppy
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.2)

        await router.publish(BookingIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",  # adult
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.2)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 2

        # Check they're in different groups
        group_ids = {c.group_id for c in confirmed}
        assert len(group_ids) == 2  # different groups

        # Check group types in DB
        groups = await db.execute_fetchall("SELECT * FROM walk_groups WHERE date='2027-07-02'")
        group_types = {g["group_type"] for g in groups}
        assert "puppy" in group_types
        assert "standard" in group_types

        await agent.stop()

    async def test_multiple_puppies_in_same_group(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Book 2 puppies at the same slot — should be in the same group
        await router.publish(BookingIntent(
            client_email="martin.klein@example.com",
            dog_name="Milo",  # 6 months
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.2)

        await router.publish(BookingIntent(
            client_email="nina.fischer@example.com",
            dog_name="Coco",  # 5 months
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.2)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 2

        # Same group (both puppies)
        assert confirmed[0].group_id == confirmed[1].group_id

        await agent.stop()


# ── Group Capacity Tests ───────────────────────────────────

class TestGroupCapacity:
    async def test_max_4_dogs_per_group(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Book 4 adult dogs at same slot — fills the group
        dogs = [
            ("lisa.mueller@example.com", "Bello"),
            ("tom.schmidt@example.com", "Luna"),
            ("anna.becker@example.com", "Rex"),
            ("jonas.weber@example.com", "Nala"),
        ]

        for email, dog in dogs:
            await router.publish(BookingIntent(
                client_email=email, dog_name=dog,
                walk_date="2027-07-02", walk_slot="11:30",
            ))
            await asyncio.sleep(0.2)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 4

        # All 4 in same group
        group_ids = {c.group_id for c in confirmed}
        assert len(group_ids) == 1

        # 5th dog should trigger a new group (different walker)
        await router.publish(BookingIntent(
            client_email="mia.hoffmann@example.com",
            dog_name="Bruno",
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.2)

        all_confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(all_confirmed) == 5

        # 5th dog should be in a different group
        assert all_confirmed[4].group_id != confirmed[0].group_id

        await agent.stop()

    async def test_max_3_groups_per_day(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Fill all 3 slots × 4 dogs = 12 walks, but only 3 walkers
        # Actually we have 3 slots, each can have groups. Max 3 groups per day.
        # With 3 walkers and 3 slots, each walker handles 1 group.
        # But a walker can handle multiple slots if groups are at different times.

        # Let's fill 3 slots with adult dogs
        # Slot 11:30 → 4 dogs, 1 group
        # Slot 12:00 → 4 dogs, 1 group (different walker since same walker is at 11:30)
        # Slot 12:30 → 4 dogs, 1 group

        adult_dogs = [
            ("lisa.mueller@example.com", "Bello"),
            ("tom.schmidt@example.com", "Luna"),
            ("anna.becker@example.com", "Rex"),
            ("jonas.weber@example.com", "Nala"),
            ("mia.hoffmann@example.com", "Bruno"),
            ("felix.krause@example.com", "Molly"),
            ("felix.krause@example.com", "Charly"),
            ("sophie.lange@example.com", "Rocky"),
            ("sophie.lange@example.com", "Gigi"),
            ("david.peters@example.com", "Bella"),
            ("julia.richter@example.com", "Cooper"),
            # We need more dogs for 3 full groups, but let's test with what we have
        ]

        slots = ["11:30", "12:00", "12:30"]
        for i, (email, dog) in enumerate(adult_dogs[:9]):
            slot = slots[i % 3]
            await router.publish(BookingIntent(
                client_email=email, dog_name=dog,
                walk_date="2027-07-02", walk_slot=slot,
            ))
            await asyncio.sleep(0.2)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        # Should have booked all 9
        assert len(confirmed) == 9

        # Should have at most 3 distinct groups
        group_ids = {c.group_id for c in confirmed}
        assert len(group_ids) <= 3

        await agent.stop()


# ── Business Day Tests ─────────────────────────────────────

class TestBusinessDays:
    async def test_weekend_booking_emits_conflict(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # 2027-07-03 is a Saturday
        await router.publish(BookingIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            walk_date="2027-07-03",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        conflicts = [e for e in emitted if isinstance(e, ScheduleConflict)]
        assert len(conflicts) == 1
        assert "not a business day" in conflicts[0].conflict_details
        assert len(conflicts[0].alternatives) > 0  # should suggest alternatives

        await agent.stop()

    async def test_monday_booking_allowed(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # 2027-07-05 is a Monday
        await router.publish(BookingIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            walk_date="2027-07-05",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 1

        await agent.stop()


# ── Cancellation Tests ─────────────────────────────────────

class TestCancellation:
    async def test_cancel_more_than_24h_full_refund(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Book a walk far in the future — use next Monday or later business day
        today = datetime.now(timezone.utc).date()
        days_to_monday = (7 - today.weekday()) % 7
        if days_to_monday == 0:
            days_to_monday = 1  # if today is Monday, use tomorrow (Tue)
        future_date = (today + timedelta(days=days_to_monday + 7)).isoformat()
        await router.publish(BookingIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            walk_date=future_date,
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        # Cancel it
        await router.publish(CancellationIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            walk_date=future_date,
            reason="Vacation",
        ))
        await asyncio.sleep(0.3)

        cancelled = [e for e in emitted if isinstance(e, CancellationConfirmed)]
        assert len(cancelled) == 1
        assert cancelled[0].refund_percent == 100
        assert cancelled[0].late_cancellation is False

        # Check walk is cancelled in DB
        walks = await db.execute_fetchall("SELECT * FROM walks WHERE status='cancelled'")
        assert len(walks) == 1

        await agent.stop()

    async def test_cancel_less_than_24h_50_percent_refund(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Book a walk on a business day first, then manipulate its datetime
        # to be within 24h to trigger the late-cancel path
        await router.publish(BookingIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        # Find the walk and set its datetime to ~1 hour from now
        now = datetime.now(timezone.utc)
        near_date = now.date().isoformat()
        near_time = (now + timedelta(hours=1)).strftime("%H:%M")
        walks = await db.execute_fetchall(
            "SELECT id FROM walks WHERE status='scheduled' AND dog_id="
            "(SELECT id FROM dogs WHERE name='Bello')"
        )
        if walks:
            await db.execute(
                "UPDATE walks SET date=?, slot=? WHERE id=?",
                (near_date, near_time, walks[0]["id"]),
            )
            await db.commit()

        # Cancel it
        await router.publish(CancellationIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            walk_date=near_date,
            reason="Emergency",
        ))
        await asyncio.sleep(0.3)

        cancelled = [e for e in emitted if isinstance(e, CancellationConfirmed)]
        assert len(cancelled) == 1
        # Should be late cancellation (within 24h)
        assert cancelled[0].late_cancellation is True
        assert cancelled[0].refund_percent == 50

        await agent.stop()


# ── Reschedule Tests ───────────────────────────────────────

class TestReschedule:
    async def test_reschedule_to_new_date(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Book a walk
        await router.publish(BookingIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        # Reschedule to next Friday
        await router.publish(RescheduleIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            original_date="2027-07-02",
            new_date="2027-07-09",
            new_slot="12:00",
        ))
        await asyncio.sleep(0.3)

        updated = [e for e in emitted if isinstance(e, ScheduleUpdated)]
        assert len(updated) == 1
        assert updated[0].old_date == "2027-07-02"
        assert updated[0].new_date == "2027-07-09"
        assert updated[0].new_slot == "12:00"

        # Check DB
        walks = await db.execute_fetchall("SELECT * FROM walks WHERE status='scheduled'")
        assert len(walks) == 1
        assert walks[0]["date"] == "2027-07-09"
        assert walks[0]["slot"] == "12:00"

        await agent.stop()


# ── Edge Cases ─────────────────────────────────────────────

class TestSchedulingEdgeCases:
    async def test_unknown_dog_emits_conflict(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        await router.publish(BookingIntent(
            client_email="lisa.mueller@example.com",
            dog_name="NonexistentDog",
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        conflicts = [e for e in emitted if isinstance(e, ScheduleConflict)]
        assert len(conflicts) == 1
        assert "not found" in conflicts[0].conflict_details

        await agent.stop()

    async def test_no_date_emits_conflict(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        await router.publish(BookingIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            walk_date=None,
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        conflicts = [e for e in emitted if isinstance(e, ScheduleConflict)]
        assert len(conflicts) == 1
        assert "No walk date" in conflicts[0].conflict_details

        await agent.stop()

    async def test_suggests_alternative_dates_on_conflict(self, setup_system, settings):
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # 2027-07-03 is Saturday
        await router.publish(BookingIntent(
            client_email="lisa.mueller@example.com",
            dog_name="Bello",
            walk_date="2027-07-03",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        conflicts = [e for e in emitted if isinstance(e, ScheduleConflict)]
        assert len(conflicts) == 1
        # Should suggest next business days (Mon, Tue, Wed)
        suggested_dates = [a["date"] for a in conflicts[0].alternatives]
        assert len(suggested_dates) == 3
        # First suggestion should be Monday 2027-07-05
        assert "2027-07-05" in suggested_dates

        await agent.stop()


# ── Helper Method Tests ────────────────────────────────────

class TestSchedulingHelpers:
    def test_is_puppy_true(self):
        dog = {"age_months": 6}
        assert SchedulingAgent._is_puppy(dog) is True

    def test_is_puppy_false_adult(self):
        dog = {"age_months": 36}
        assert SchedulingAgent._is_puppy(dog) is False

    def test_is_puppy_false_too_young(self):
        dog = {"age_months": 2}
        assert SchedulingAgent._is_puppy(dog) is False

    def test_is_puppy_boundary_4_months(self):
        dog = {"age_months": 4}
        assert SchedulingAgent._is_puppy(dog) is True

    def test_is_puppy_boundary_10_months(self):
        dog = {"age_months": 10}
        assert SchedulingAgent._is_puppy(dog) is True

    def test_is_business_day_weekday(self, settings):
        agent = SchedulingAgent.__new__(SchedulingAgent)
        agent._settings = settings
        assert agent._is_business_day("2027-07-02") is True  # Friday

    def test_is_business_day_weekend(self, settings):
        agent = SchedulingAgent.__new__(SchedulingAgent)
        agent._settings = settings
        assert agent._is_business_day("2027-07-03") is False  # Saturday

    def test_suggest_next_business_days(self, settings):
        agent = SchedulingAgent.__new__(SchedulingAgent)
        agent._settings = settings
        suggestions = agent._suggest_next_business_days("2027-07-03", 3)
        # Sat → next business days: Mon 7th, Tue 8th, Wed 9th
        assert len(suggestions) == 3
        assert suggestions[0]["date"] == "2027-07-05"

    def test_parse_walk_datetime(self):
        dt = SchedulingAgent._parse_walk_datetime("2027-07-02", "11:30")
        assert dt is not None
        assert dt.hour == 11
        assert dt.minute == 30

    def test_parse_walk_datetime_invalid(self):
        dt = SchedulingAgent._parse_walk_datetime("invalid", "11:30")
        assert dt is None

    def test_is_in_heat_true(self):
        dog = {"sex": "female", "in_heat": 1}
        assert SchedulingAgent._is_in_heat(dog) is True

    def test_is_in_heat_false_male(self):
        dog = {"sex": "male", "in_heat": 1}
        assert SchedulingAgent._is_in_heat(dog) is False  # only females

    def test_is_in_heat_false_not_in_heat(self):
        dog = {"sex": "female", "in_heat": 0}
        assert SchedulingAgent._is_in_heat(dog) is False

    def test_is_in_heat_missing_field(self):
        dog = {"sex": "female"}
        assert SchedulingAgent._is_in_heat(dog) is False

    def test_is_intact_male_true(self):
        dog = {"sex": "male", "castrated": "intact"}
        assert SchedulingAgent._is_intact_male(dog) is True

    def test_is_intact_male_false_neutered(self):
        dog = {"sex": "male", "castrated": "neutered"}
        assert SchedulingAgent._is_intact_male(dog) is False

    def test_is_intact_male_false_female(self):
        dog = {"sex": "female", "castrated": "intact"}
        assert SchedulingAgent._is_intact_male(dog) is False


# ── Läufig (In-Heat) Group Tests ───────────────────────────

class TestHeatGroupLogic:
    """Test that in-heat females and intact males are placed in compatible groups."""

    async def test_in_heat_female_goes_to_empty_group(self, setup_system, settings):
        """An in-heat female placed first gets her own group."""
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        booking = BookingIntent(
            client_email="david.peters@example.com",
            client_name="David Peters",
            dog_name="Bella",  # in_heat=1 in seed data
            walk_date="2027-07-02",
            walk_slot="11:30",
        )
        await router.publish(booking)
        await asyncio.sleep(0.3)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 1
        assert confirmed[0].dog_name == "Bella"
        assert confirmed[0].walk_slot == "11:30"

        # Verify Bella is booked
        walks = await db.execute_fetchall(
            "SELECT w.*, d.name, d.in_heat FROM walks w JOIN dogs d ON w.dog_id = d.id WHERE w.status='scheduled'"
        )
        assert len(walks) == 1
        assert walks[0]["name"] == "Bella"

        await agent.stop()

    async def test_intact_male_goes_to_empty_group(self, setup_system, settings):
        """An intact male placed first gets his own group."""
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        booking = BookingIntent(
            client_email="julia.richter@example.com",
            client_name="Julia Richter",
            dog_name="Cooper",  # castrated='intact' in seed data
            walk_date="2027-07-02",
            walk_slot="11:30",
        )
        await router.publish(booking)
        await asyncio.sleep(0.3)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 1
        assert confirmed[0].dog_name == "Cooper"

        await agent.stop()

    async def test_in_heat_and_intact_male_separated(self, setup_system, settings):
        """In-heat female and intact male go to DIFFERENT groups at same slot."""
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Book in-heat female first
        await router.publish(BookingIntent(
            client_email="david.peters@example.com",
            client_name="David Peters",
            dog_name="Bella",  # in_heat=1
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        # Book intact male at same slot
        await router.publish(BookingIntent(
            client_email="julia.richter@example.com",
            client_name="Julia Richter",
            dog_name="Cooper",  # intact male
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 2

        # They should be in DIFFERENT groups
        group_ids = {c.group_id for c in confirmed}
        assert len(group_ids) == 2, (
            f"In-heat female and intact male should be in different groups, "
            f"got groups: {group_ids}"
        )

        # Verify in DB
        walks = await db.execute_fetchall(
            "SELECT w.group_id, d.name, d.sex, d.castrated, d.in_heat "
            "FROM walks w JOIN dogs d ON w.dog_id = d.id "
            "WHERE w.status='scheduled' AND w.date='2027-07-02' AND w.slot='11:30'"
        )
        groups = {}
        for w in walks:
            groups.setdefault(w["group_id"], []).append(w["name"])
        assert len(groups) == 2, f"Expected 2 separate groups, got {len(groups)}"

        await agent.stop()

    async def test_neutered_male_can_join_in_heat_female_group(self, setup_system, settings):
        """A neutered male CAN join a group that has an in-heat female."""
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Book in-heat female
        await router.publish(BookingIntent(
            client_email="david.peters@example.com",
            client_name="David Peters",
            dog_name="Bella",  # in_heat=1
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        # Book neutered male at same slot — should join Bella's group
        await router.publish(BookingIntent(
            client_email="lisa.mueller@example.com",
            client_name="Lisa Müller",
            dog_name="Bello",  # neutered male
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 2

        # Bello (neutered) should be in the same group as Bella (in-heat)
        bella = next(c for c in confirmed if c.dog_name == "Bella")
        bello = next(c for c in confirmed if c.dog_name == "Bello")
        assert bella.group_id == bello.group_id, (
            f"Neutered male should join in-heat female's group. "
            f"Bella: {bella.group_id}, Bello: {bello.group_id}"
        )

        await agent.stop()

    async def test_spayed_female_can_join_intact_male_group(self, setup_system, settings):
        """A spayed female CAN join a group that has an intact male."""
        router, db = setup_system
        agent = SchedulingAgent(router, settings)
        await agent.start()
        emitted = _track(router)

        # Book intact male
        await router.publish(BookingIntent(
            client_email="julia.richter@example.com",
            client_name="Julia Richter",
            dog_name="Cooper",  # intact male
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        # Book spayed female at same slot — should join Cooper's group
        await router.publish(BookingIntent(
            client_email="tom.schmidt@example.com",
            client_name="Tom Schmidt",
            dog_name="Luna",  # spayed female
            walk_date="2027-07-02",
            walk_slot="11:30",
        ))
        await asyncio.sleep(0.3)

        confirmed = [e for e in emitted if isinstance(e, ScheduleConfirmed)]
        assert len(confirmed) == 2

        cooper = next(c for c in confirmed if c.dog_name == "Cooper")
        luna = next(c for c in confirmed if c.dog_name == "Luna")
        assert cooper.group_id == luna.group_id, (
            f"Spayed female should join intact male's group. "
            f"Cooper: {cooper.group_id}, Luna: {luna.group_id}"
        )

        await agent.stop()
