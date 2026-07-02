"""Tests for the EventBus — pub/sub, backpressure, retry, DLQ, replay."""

from __future__ import annotations

import asyncio

import pytest

from src.bus.bus import EventBus, WILDCARD
from src.bus.event import BookingIntent, ConfirmationSent, EmailReceived, JournalEntry
from src.bus.store import EventStore


@pytest.fixture
async def bus(tmp_db_path):
    store = EventStore(tmp_db_path)
    await store.init()
    b = EventBus(store)
    await b.start()
    yield b
    await b.stop()
    await store.close()


class TestEventBusSubscribe:
    async def test_subscribed_handler_receives_event(self, bus):
        received: list = []

        async def handler(event):
            received.append(event)

        bus.subscribe("BookingIntent", handler)
        ev = BookingIntent(client_email="test@example.com")
        await bus.publish(ev)

        await asyncio.sleep(0.1)
        assert len(received) == 1
        assert received[0].client_email == "test@example.com"

    async def test_unrelated_type_not_received(self, bus):
        received: list = []

        async def handler(event):
            received.append(event)

        bus.subscribe("BookingIntent", handler)
        ev = ConfirmationSent(to_email="a@b.com", subject="s", body="b")
        await bus.publish(ev)

        await asyncio.sleep(0.1)
        assert len(received) == 0

    async def test_wildcard_receives_all(self, bus):
        received: list = []

        async def handler(event):
            received.append(event)

        bus.subscribe(WILDCARD, handler)
        await bus.publish(BookingIntent(client_email="a@b.com"))
        await bus.publish(ConfirmationSent(to_email="a@b.com", subject="s", body="b"))

        await asyncio.sleep(0.15)
        assert len(received) == 2

    async def test_multiple_subscribers_same_type(self, bus):
        received_a: list = []
        received_b: list = []

        async def handler_a(event):
            received_a.append(event)

        async def handler_b(event):
            received_b.append(event)

        bus.subscribe("BookingIntent", handler_a, subscriber_name="A")
        bus.subscribe("BookingIntent", handler_b, subscriber_name="B")

        await bus.publish(BookingIntent(client_email="a@b.com"))
        await asyncio.sleep(0.1)

        assert len(received_a) == 1
        assert len(received_b) == 1


class TestEventBusDurability:
    async def test_event_persisted_before_dispatch(self, bus):
        async def handler(event):
            pass

        bus.subscribe("BookingIntent", handler)
        ev = BookingIntent(client_email="persisted@example.com")
        await bus.publish(ev)

        # Check it's in the store
        stats = await bus.store.get_stats()
        # Should be done or pending
        total = sum(stats.values())
        assert total >= 1

    async def test_event_marked_done_after_handler(self, bus):
        async def handler(event):
            pass  # success

        bus.subscribe("BookingIntent", handler)
        ev = BookingIntent(client_email="done@example.com")
        await bus.publish(ev)
        await asyncio.sleep(0.15)

        stats = await bus.store.get_stats()
        assert stats.get("done", 0) >= 1


class TestEventBusRetry:
    async def test_failed_event_retries_then_dlq(self, bus):
        call_count = 0

        async def failing_handler(event):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Intentional failure")

        bus.subscribe("BookingIntent", failing_handler)
        ev = BookingIntent(client_email="fail@example.com")
        await bus.publish(ev)

        # Wait for retries to exhaust (3 retries with backoff: 1s + 2s + 4s = 7s)
        await asyncio.sleep(10.0)

        # The event should eventually end up in DLQ
        dlq_items = await bus.store.get_dlq()
        assert len(dlq_items) >= 1
        assert any("Intentional failure" in item["error"] for item in dlq_items)


class TestEventBusReplay:
    async def test_pending_events_replayed_on_start(self, tmp_db_path):
        """Events saved but not processed should be replayed when bus restarts."""
        store = EventStore(tmp_db_path)
        await store.init()

        # Save an event without starting the bus
        ev = BookingIntent(client_email="replay@example.com", dog_name="Bello")
        await store.save(ev)
        await store.close()

        # Now create a new bus and start it — should replay pending
        store2 = EventStore(tmp_db_path)
        await store2.init()
        bus2 = EventBus(store2)

        received: list = []

        async def handler(event):
            received.append(event)

        bus2.subscribe("BookingIntent", handler)
        await bus2.start()

        await asyncio.sleep(0.3)
        assert len(received) == 1
        assert received[0].dog_name == "Bello"

        await bus2.stop()
        await store2.close()


class TestEventBusBackpressure:
    async def test_backpressure_when_queue_full(self, bus):
        """When subscriber queue is full, publish should await, not drop."""
        processed: list = []

        async def slow_handler(event):
            await asyncio.sleep(0.05)  # slow handler
            processed.append(event)

        # Subscribe with tiny queue
        bus.subscribe("BookingIntent", slow_handler, queue_size=2)

        # Publish more events than queue can hold
        for i in range(5):
            await bus.publish(BookingIntent(client_email=f"bp{i}@example.com"))

        # Wait for all to process
        await asyncio.sleep(1.0)
        assert len(processed) == 5  # none dropped
