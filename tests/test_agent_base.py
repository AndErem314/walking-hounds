"""Tests for the BaseAgent abstract class."""

from __future__ import annotations

import asyncio

import pytest

from src.agents.base import BaseAgent
from src.router.router import EventRouter
from src.router.event import BookingIntent, ConfirmationSent, BaseEvent
from src.router.store import EventStore


# ── Test agent implementations ─────────────────────────────

class SimpleAgent(BaseAgent):
    """Records all events it receives."""
    name = "SimpleAgent"

    def __init__(self, router):
        super().__init__(router)
        self.received: list[BaseEvent] = []

    def subscribed_event_types(self) -> list[str]:
        return ["BookingIntent"]

    async def handle(self, event: BaseEvent) -> None:
        self.received.append(event)


class WildcardAgent(BaseAgent):
    """Subscribes to all events."""
    name = "WildcardAgent"

    def __init__(self, router):
        super().__init__(router)
        self.received: list[BaseEvent] = []

    def subscribed_event_types(self) -> list[str]:
        return ["*"]

    async def handle(self, event: BaseEvent) -> None:
        self.received.append(event)


class FailingAgent(BaseAgent):
    """Always raises an error."""
    name = "FailingAgent"

    def subscribed_event_types(self) -> list[str]:
        return ["BookingIntent"]

    async def handle(self, event: BaseEvent) -> None:
        raise RuntimeError("Agent failure for test")


class ConfirmationReceiver(BaseAgent):
    """Receives ConfirmationSent events."""
    name = "ConfirmationReceiver"

    def __init__(self, router):
        super().__init__(router)
        self.received: list[BaseEvent] = []

    def subscribed_event_types(self) -> list[str]:
        return ["ConfirmationSent"]

    async def handle(self, event: BaseEvent) -> None:
        self.received.append(event)


class EmittingAgent(BaseAgent):
    """Emits a new event when it receives one."""
    name = "EmittingAgent"

    def __init__(self, router):
        super().__init__(router)
        self.emit_count = 0

    def subscribed_event_types(self) -> list[str]:
        return ["BookingIntent"]

    async def handle(self, event: BaseEvent) -> None:
        await self.emit(
            ConfirmationSent(
                to_email="out@example.com",
                subject="Confirmed",
                body="Body",
                booking_id="test-booking",
            )
        )
        self.emit_count += 1


# ── Fixtures ───────────────────────────────────────────────

@pytest.fixture
async def router(tmp_db_path):
    store = EventStore(tmp_db_path)
    await store.init()
    b = EventRouter(store)
    await b.start()
    yield b
    await b.stop()
    await store.close()


# ── Tests ──────────────────────────────────────────────────

class TestBaseAgentLifecycle:
    async def test_agent_start_stop(self, router):
        agent = SimpleAgent(router)
        await agent.start()
        assert agent.health.status == "running"
        await agent.stop()
        assert agent.health.status == "stopped"

    async def test_agent_receives_subscribed_event(self, router):
        agent = SimpleAgent(router)
        await agent.start()

        ev = BookingIntent(client_email="test@example.com")
        await router.publish(ev)
        await asyncio.sleep(0.2)

        assert len(agent.received) == 1
        assert agent.received[0].client_email == "test@example.com"

        await agent.stop()

    async def test_agent_ignores_unsubscribed_event(self, router):
        agent = SimpleAgent(router)
        await agent.start()

        ev = ConfirmationSent(to_email="a@b.com", subject="s", body="b")
        await router.publish(ev)
        await asyncio.sleep(0.2)

        assert len(agent.received) == 0
        await agent.stop()

    async def test_wildcard_agent_receives_all(self, router):
        agent = WildcardAgent(router)
        await agent.start()

        await router.publish(BookingIntent(client_email="a@b.com"))
        await router.publish(ConfirmationSent(to_email="a@b.com", subject="s", body="b"))
        await asyncio.sleep(0.2)

        assert len(agent.received) == 2
        await agent.stop()


class TestBaseAgentHealth:
    async def test_health_increments_processed_count(self, router):
        agent = SimpleAgent(router)
        await agent.start()

        await router.publish(BookingIntent(client_email="a@b.com"))
        await asyncio.sleep(0.2)

        assert agent.health.processed_count == 1
        await agent.stop()

    async def test_health_records_errors(self, router):
        agent = FailingAgent(router)
        await agent.start()

        await router.publish(BookingIntent(client_email="a@b.com"))
        await asyncio.sleep(1.0)  # wait for retries

        assert agent.health.error_count > 0
        assert "Agent failure for test" in agent.health.last_error
        await agent.stop()

    async def test_health_to_dict(self, router):
        agent = SimpleAgent(router)
        await agent.start()

        d = agent.health.to_dict()
        assert d["name"] == "SimpleAgent"
        assert d["status"] == "running"
        assert "processed_count" in d
        assert "error_count" in d

        await agent.stop()


class TestBaseAgentEmit:
    async def test_agent_emits_event(self, router):
        emitter = EmittingAgent(router)
        receiver = ConfirmationReceiver(router)

        await emitter.start()
        await receiver.start()

        await router.publish(BookingIntent(client_email="a@b.com"))
        await asyncio.sleep(0.3)

        assert emitter.emit_count == 1
        assert len(receiver.received) == 1
        assert isinstance(receiver.received[0], ConfirmationSent)

        await emitter.stop()
        await receiver.stop()


class TestBaseAgentOnStartOnStop:
    async def test_on_start_called(self, router):
        called = []

        class HookAgent(BaseAgent):
            name = "HookAgent"

            def subscribed_event_types(self):
                return []

            async def handle(self, event):
                pass

            async def on_start(self):
                called.append("start")

            async def on_stop(self):
                called.append("stop")

        agent = HookAgent(router)
        await agent.start()
        await agent.stop()

        assert called == ["start", "stop"]
