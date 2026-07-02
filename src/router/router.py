"""Async event router — pub/sub with backpressure, durability, and retry.

Core design:
- publish() persists to SQLite, then enqueues to subscriber queues.
- Each subscriber gets its own bounded asyncio.Queue.
- If a queue is full, publish() awaits (natural backpressure).
- Handlers run as asyncio tasks; failures retry up to max_retries,
  then the event goes to the dead-letter queue.
- On restart, pending/processing events are replayed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Awaitable, Callable, Type

from .event import BaseEvent, event_type_name
from .store import EventStore

logger = logging.getLogger(__name__)

Handler = Callable[[BaseEvent], Awaitable[None]]

# Special type name for "subscribe to everything"
WILDCARD = "*"


class EventRouter:
    """In-memory async pub/sub backed by SQLite durability."""

    def __init__(
        self,
        store: EventStore,
        *,
        default_queue_size: int = 100,
    ):
        self._store = store
        self._default_queue_size = default_queue_size
        self._subscribers: dict[str, list[tuple[asyncio.Queue, Handler, str]]] = (
            defaultdict(list)
        )
        self._tasks: list[asyncio.Task] = []
        self._running = False

    # ── Subscription ────────────────────────────────────────

    def subscribe(
        self,
        event_type: str,
        handler: Handler,
        *,
        subscriber_name: str = "",
        queue_size: int | None = None,
    ) -> None:
        """Register *handler* to receive events of *event_type*.

        Use WILDCARD ('*') to subscribe to all events (for Logger Agent).
        """
        q: asyncio.Queue[BaseEvent] = asyncio.Queue(
            maxsize=queue_size or self._default_queue_size
        )
        self._subscribers[event_type].append((q, handler, subscriber_name or handler.__name__))
        if self._running:
            self._start_worker(event_type, q, handler, subscriber_name)

    def _start_worker(
        self,
        event_type: str,
        queue: asyncio.Queue,
        handler: Handler,
        name: str,
    ) -> None:
        task = asyncio.create_task(
            self._worker(event_type, queue, handler, name),
            name=f"router-{event_type}-{name}",
        )
        self._tasks.append(task)

    async def _worker(
        self,
        event_type: str,
        queue: asyncio.Queue,
        handler: Handler,
        name: str,
    ) -> None:
        """Consumer loop: pull events from queue, call handler, handle errors."""
        while True:
            event = await queue.get()
            if event is None:
                # Shutdown signal
                break
            try:
                await self._store.mark_processing(event.id)
                await handler(event)
                await self._store.mark_done(event.id)
            except Exception as exc:
                logger.warning(
                    "Handler %s failed on %s (id=%s): %s",
                    name, event_type, event.id, exc,
                )
                await self._store.mark_failed(event.id, str(exc))
                # Check if retries exhausted → DLQ, else re-enqueue
                rows = await self._store.db.execute_fetchall(
                    "SELECT status, retries, max_retries FROM event_store WHERE id=?",
                    (event.id,),
                )
                if rows:
                    r = rows[0]
                    if r["status"] == "failed":
                        await self._store.move_to_dlq(event.id)
                        logger.warning(
                            "Event %s moved to DLQ after %d retries",
                            event.id, r["retries"],
                        )
                    else:
                        # Still under retry limit — re-enqueue with delay
                        delay = 2 ** r["retries"]  # 1s, 2s, 4s...
                        logger.info(
                            "Re-enqueueing %s for retry %d/%d in %ds",
                            event.id, r["retries"], r["max_retries"], delay,
                        )
                        await asyncio.sleep(delay)
                        await queue.put(event)
            finally:
                queue.task_done()

    # ── Publishing ──────────────────────────────────────────

    async def publish(self, event: BaseEvent) -> None:
        """Persist event, then dispatch to all matching subscribers."""
        # 1. Durable write first
        await self._store.save(event)

        # 2. Dispatch to typed subscribers
        type_name = event_type_name(event)
        await self._dispatch(type_name, event)

        # 3. Dispatch to wildcard subscribers
        if WILDCARD in self._subscribers:
            await self._dispatch(WILDCARD, event)

    async def _dispatch(self, key: str, event: BaseEvent) -> None:
        """Enqueue event to all subscribers of *key*, with backpressure."""
        for queue, handler, name in self._subscribers.get(key, []):
            try:
                # put_nowait if there's room, else await (backpressure)
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.debug("Backpressure: queue full for %s/%s, awaiting...", key, name)
                await queue.put(event)

    # ── Lifecycle ───────────────────────────────────────────

    async def start(self) -> None:
        """Start all worker tasks.  Also replay pending events from the store."""
        self._running = True

        # Start worker tasks for all current subscriptions
        for event_type, subs in self._subscribers.items():
            for queue, handler, name in subs:
                self._start_worker(event_type, queue, handler, name)

        # Replay pending events
        pending = await self._store.get_pending()
        if pending:
            logger.info("Replaying %d pending events from store", len(pending))
            for event in pending:
                type_name = event_type_name(event)
                await self._dispatch(type_name, event)
                if WILDCARD in self._subscribers:
                    await self._dispatch(WILDCARD, event)

    async def stop(self) -> None:
        """Graceful shutdown: signal workers to stop, wait for them."""
        self._running = False

        # Send None (poison pill) to all queues
        for subs in self._subscribers.values():
            for queue, _, _ in subs:
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass  # Worker will finish current and check running flag

        # Cancel any still-running tasks after a short wait
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()

    async def replay(self, event: BaseEvent) -> None:
        """Re-dispatch a single event (used for DLQ replay via dashboard)."""
        await self._store.save(event)  # re-save with new id
        type_name = event_type_name(event)
        await self._dispatch(type_name, event)
        if WILDCARD in self._subscribers:
            await self._dispatch(WILDCARD, event)

    @property
    def store(self) -> EventStore:
        return self._store
