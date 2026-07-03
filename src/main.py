"""Walking Hounds — entry point.

Starts the event router, initialises the database, runs all agents,
and serves the FastAPI dashboard until interrupted (SIGINT / SIGTERM).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .router.router import EventRouter
from .router.store import EventStore
from .config import get_settings
from .db.database import init_database, close_database

logger = logging.getLogger(__name__)


async def run() -> None:
    """Boot the full system: store → router → agents → dashboard → wait for shutdown."""
    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    # ── Init database (business tables) ────────────────────
    db = await init_database(settings.db_path)
    logger.info("Database ready: %s", settings.db_path)

    # ── Init event store + router ──────────────────────────
    store = EventStore(settings.db_path)
    await store.init()
    router = EventRouter(store)

    # ── Register agents ────────────────────────────────────
    from .agents.intake import IntakeAgent
    from .agents.onboarding import OnboardingAgent
    from .agents.scheduling import SchedulingAgent
    from .agents.communication import CommunicationAgent
    from .agents.invoicing import InvoicingAgent
    from .agents.reminder import ReminderAgent
    from .agents.logger import LoggerAgent

    agents = [
        IntakeAgent(router, settings),
        OnboardingAgent(router, settings),
        SchedulingAgent(router, settings),
        CommunicationAgent(router, settings),
        InvoicingAgent(router, settings),
        ReminderAgent(router, settings),
        LoggerAgent(router),
    ]

    # ── Start router ───────────────────────────────────────
    await router.start()
    logger.info("Event router started — %d agents registered", len(agents))

    for agent in agents:
        await agent.start()

    # ── Start dashboard ────────────────────────────────────
    from .dashboard.app import create_dashboard_app
    import uvicorn

    app = create_dashboard_app(router, settings)
    config = uvicorn.Config(
        app,
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level="warning",
    )
    server = uvicorn.Server(config)
    dashboard_task = asyncio.create_task(server.serve(), name="dashboard")
    logger.info("Dashboard started on http://%s:%d", settings.dashboard_host, settings.dashboard_port)

    # ── Wait for shutdown signal ───────────────────────────
    stop_event = asyncio.Event()

    def _signal_handler(sig, frame):
        logger.info("Received signal %s — shutting down", sig)
        stop_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    await stop_event.wait()

    # ── Graceful shutdown ───────────────────────────────────
    logger.info("Stopping dashboard...")
    server.should_exit = True
    try:
        await asyncio.wait_for(dashboard_task, timeout=5.0)
    except asyncio.TimeoutError:
        dashboard_task.cancel()

    logger.info("Stopping agents...")
    for agent in reversed(agents):
        await agent.stop()

    logger.info("Stopping event router...")
    await router.stop()
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
