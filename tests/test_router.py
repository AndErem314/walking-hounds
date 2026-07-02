"""Tests for the EventRouter — pub/sub, backpressure, retry, DLQ, replay."""

from __future__ import annotations

import asyncio

import pytest

from src.router.router import EventRouter, WILDCARD
from src.router.event import BookingIntent, ConfirmationSent, EmailReceived, JournalEntry
from src.router.store import EventStore


@pytest.fixture
async def router(tmp_db_path):
    store = EventStore(tmp_db_path)
    await store.init()
    b = EventRouter(store)
    await b.start()
    yield b
    await b.stop()
    await store.close()


class TestEventRouterSubscribe:
    async def test_subscribed_handler_receives_event(self, router):
        received: list = []

        async def handler(event):
            received.append(event)

        router.subscribe("BookingIntent", handler)
        ev = BookingIntent(client_email="test@example.com")
        await router.publish(ev)

        await asyncio.sleep(0.1)
        assert len(received) == 1
        assert received[0].client_email == "test@example.com"

    async def test_unrelated_type_not_received(self, router):
        received: list = []

        async def handler(event):
            received.append(event)

        router.subscribe("BookingIntent", handler)
        ev = ConfirmationSent(to_email="a@b.com", subject="s", body="b")
        await router.publish(ev)

        await asyncio.sleep(0.1)
        assert len(received) == 0

    async def test_wildcard_receives_all(self, router):
        received: list = []

        async def handler(event):
            received.append(event)

        router.subscribe(WILDCARD, handler)
        await router.publish(BookingIntent(client_email="a@b.com"))
        await router.publish(ConfirmationSent(to_email="a@b.com", subject="s", body="b"))

        await asyncio.sleep(0.15)
        assert len(received) == 2

    async def test_multiple_subscribers_same_type(self, router):
        received_a: list = []
        received_b: list = []

        async def handler_a(event):
            received_a.append(event)

        async def handler_b(event):
            received_b.append(event)

        router.subscribe("BookingIntent", handler_a, subscriber_name="A")
        router.subscribe("BookingIntent", handler_b, subscriber_name="B")

        await router.publish(BookingIntent(client_email="a@b.com"))
        await asyncio.sleep(0.1)

        assert len(received_a) == 1
        assert len(received_b) == 1


class TestEventRouterDurability:
    async def test_event_persisted_before_dispatch(self, router):
        async def handler(event):
            pass

        router.subscribe("BookingIntent", handler)
        ev = BookingIntent(client_email="persisted@example.com")
        await router.publish(ev)

        # Check it's in the store
        stats = await router.store.get_stats()
        # Should be done or pending
        total = sum(stats.values())
        assert total >= 1

    async def test_event_marked_done_after_handler(self, router):
        async def handler(event):
            pass  # success

        router.subscribe("BookingIntent", handler)
        ev = BookingIntent(client_email="done@example.com")
        await router.publish(ev)
        await asyncio.sleep(0.15)

        stats = await router.store.get_stats()
        assert stats.get("done", 0) >= 1


class TestEventRouterRetry:
    async def test_failed_event_retries_then_dlq(self, router):
        call_count = 0

        async def failing_handler(event):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Intentional failure")

        router.subscribe("BookingIntent", failing_handler)
        ev = BookingIntent(client_email="fail@example.com")
        await router.publish(ev)

        # Wait for retries to exhaust (3 retries with backoff: 1s + 2s + 4s = 7s)
        await asyncio.sleep(10.0)

        # The event should eventually end up in DLQ
        dlq_items = await router.store.get_dlq()
        assert len(dlq_items) >= 1
        assert any("Intentional failure" in item["error"] for item in dlq_items)


class TestEventRouterReplay:
    async def test_pending_events_replayed_on_start(self, tmp_db_path):
        """Events saved but not processed should be replayed when router restarts."""
        store = EventStore(tmp_db_path)
        await store.init()

        # Save an event without starting the router
        ev = BookingIntent(client_email="replay@example.com", dog_name="Bello")
        await store.save(ev)
        await store.close()

        # Now create a new router and start it — should replay pending
        store2 = EventStore(tmp_db_path)
        await store2.init()
        bus2 = EventRouter(store2)

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


class TestEventRouterBackpressure:
    async def test_backpressure_when_queue_full(self, router):
        """When subscriber queue is full, publish should await, not drop."""
        processed: list = []

        async def slow_handler(event):
            await asyncio.sleep(0.05)  # slow handler
            processed.append(event)

        # Subscribe with tiny queue
        router.subscribe("BookingIntent", slow_handler, queue_size=2)

        # Publish more events than queue can hold
        for i in range(5):
            await router.publish(BookingIntent(client_email=f"bp{i}@example.com"))

        # Wait for all to process
        await asyncio.sleep(1.0)
        assert len(processed) == 5  # none dropped
