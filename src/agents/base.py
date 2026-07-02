"""Abstract base agent — async lifecycle, health reporting, event handling.

Every agent in Walking Hounds inherits from BaseAgent.  The agent runs as
an asyncio task with a bounded queue.  It subscribes to specific event
types on the bus and emits events via the bus.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from ..bus.bus import EventBus
from ..bus.event import BaseEvent

logger = logging.getLogger(__name__)


class AgentHealth:
    """Snapshot of an agent's runtime state for the dashboard."""

    def __init__(self, name: str):
        self.name = name
        self.status: str = "stopped"      # stopped / starting / running / error
        self.queue_depth: int = 0
        self.processed_count: int = 0
        self.error_count: int = 0
        self.last_processed: datetime | None = None
        self.last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "queue_depth": self.queue_depth,
            "processed_count": self.processed_count,
            "error_count": self.error_count,
            "last_processed": self.last_processed.isoformat() if self.last_processed else None,
            "last_error": self.last_error,
        }


class BaseAgent(ABC):
    """Base class for all Walking Hounds agents.

    Subclasses must:
    - Define `name` (class attribute)
    - Implement `handle(event)` — process a single event
    - Optionally override `subscribed_event_types()` to declare interests
    - Optionally override `on_start()` / `on_stop()` for lifecycle hooks
    """

    name: str = "BaseAgent"

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._health = AgentHealth(self.name)
        self._inbox: asyncio.Queue[BaseEvent | None] = asyncio.Queue(maxsize=200)
        self._task: asyncio.Task | None = None

    # ── Override points ─────────────────────────────────────

    @abstractmethod
    async def handle(self, event: BaseEvent) -> None:
        """Process a single event.  Raise on failure (triggers retry)."""
        ...

    def subscribed_event_types(self) -> list[str]:
        """Return event type names this agent wants to receive.
        Return ['*'] to subscribe to all events (wildcard).
        Override in subclasses; default is no subscriptions."""
        return []

    async def on_start(self) -> None:
        """Called once before the main loop starts.  Override for setup."""
        pass

    async def on_stop(self) -> None:
        """Called once after the main loop stops.  Override for cleanup."""
        pass

    # ── Lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        """Register subscriptions on the bus and start the main loop."""
        self._health.status = "starting"
        logger.info("Agent %s starting...", self.name)

        for event_type in self.subscribed_event_types():
            self._bus.subscribe(
                event_type,
                self._enqueue,
                subscriber_name=self.name,
                queue_size=200,
            )

        await self.on_start()

        self._task = asyncio.create_task(self._run(), name=f"agent-{self.name}")
        self._health.status = "running"
        logger.info("Agent %s running", self.name)

    async def _enqueue(self, event: BaseEvent) -> None:
        """Called by the bus — just put the event in our inbox."""
        await self._inbox.put(event)

    async def _run(self) -> None:
        """Main loop: pull from inbox, call handle(), update health."""
        while True:
            event = await self._inbox.get()
            if event is None:
                # Shutdown signal
                break
            self._health.queue_depth = self._inbox.qsize()
            try:
                await self.handle(event)
                self._health.processed_count += 1
                self._health.last_processed = datetime.now(timezone.utc)
            except Exception as exc:
                self._health.error_count += 1
                self._health.last_error = str(exc)
                logger.warning(
                    "Agent %s failed handling %s: %s",
                    self.name, type(event).__name__, exc,
                )
                # Don't re-raise — let the bus worker handle retry/DLQ
            finally:
                self._inbox.task_done()

        self._health.status = "stopped"

    async def stop(self) -> None:
        """Signal the agent to stop and wait for cleanup."""
        logger.info("Agent %s stopping...", self.name)
        self._health.status = "stopping"

        # Poison pill
        try:
            self._inbox.put_nowait(None)
        except asyncio.QueueFull:
            pass

        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                logger.warning("Agent %s force-cancelled after timeout", self.name)

        await self.on_stop()
        self._health.status = "stopped"
        logger.info("Agent %s stopped", self.name)

    # ── Helpers ─────────────────────────────────────────────

    async def emit(self, event: BaseEvent) -> None:
        """Publish an event on the bus."""
        await self._bus.publish(event)

    @property
    def health(self) -> AgentHealth:
        return self._health

    @property
    def bus(self) -> EventBus:
        return self._bus
