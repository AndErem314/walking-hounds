"""Walking Hounds — entry point.

Starts the event bus, initialises the database, and runs all agents
until interrupted (SIGINT / SIGTERM).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .bus.bus import EventBus
from .bus.store import EventStore
from .config import get_settings
from .db.database import init_database, close_database

logger = logging.getLogger(__name__)


async def run() -> None:
    """Boot the full system: store → bus → agents → wait for shutdown."""
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    # ── Init database (business tables) ────────────────────
    db = await init_database(settings.db_path)
    logger.info("Database ready: %s", settings.db_path)

    # ── Init event store + bus ──────────────────────────────
    store = EventStore(settings.db_path)
    await store.init()
    bus = EventBus(store)

    # ── Agents will be registered here in later phases ──────
    agents: list = []
    # from .agents.intake import IntakeAgent
    # from .agents.scheduling import SchedulingAgent
    # ... etc.
    # agents will be appended as they're implemented

    # ── Start bus ───────────────────────────────────────────
    await bus.start()
    logger.info("Event bus started — %d agents registered", len(agents))

    for agent in agents:
        await agent.start()

    # ── Wait for shutdown signal ────────────────────────────
    stop_event = asyncio.Event()

    def _signal_handler(sig, frame):
        logger.info("Received signal %s — shutting down", sig)
        stop_event.set()

    # SIGTERM is what Docker/cron sends; SIGINT is Ctrl+C
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    await stop_event.wait()

    # ── Graceful shutdown ───────────────────────────────────
    logger.info("Stopping agents...")
    for agent in reversed(agents):
        await agent.stop()

    logger.info("Stopping event bus...")
    await bus.stop()
    await store.close()
    await close_database(db)
    logger.info("Shutdown complete — goodbye!")


def main() -> None:
    """Sync entry point for console_scripts."""
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
