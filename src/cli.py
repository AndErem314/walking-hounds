"""Walking Hounds — CLI (Click).

Commands:
  walking-hounds start       Start the full system (router + all agents)
  walking-hounds init-db     Create database tables
  walking-hounds status      Show event store stats and agent health
  walking-hounds dlq         Show dead-letter queue items
  walking-hounds seed        Generate seed data (12 clients, 3 walkers)
"""

from __future__ import annotations

import asyncio
import json
import sys

import click

from .config import get_settings


@click.group()
def cli() -> None:
    """Walking Hounds — multi-agent dog-walking automation."""
    pass


@cli.command()
def start() -> None:
    """Start the full system."""
    from .main import main as _main
    _main()


@cli.command()
def init_db() -> None:
    """Create database tables."""
    from .db.database import init_database, close_database
    from .router.store import EventStore

    settings = get_settings()

    async def _run():
        db = await init_database(settings.db_path)
        store = EventStore(settings.db_path)
        await store.init()
        click.echo(f"✓ Database initialised: {settings.db_path}")
        click.echo(f"✓ Event store ready")
        await store.close()
        await close_database(db)

    asyncio.run(_run())


@cli.command()
def status() -> None:
    """Show event store statistics."""
    from .router.store import EventStore

    settings = get_settings()

    async def _run():
        store = EventStore(settings.db_path)
        await store.init()
        stats = await store.get_stats()
        if not stats:
            click.echo("Event store is empty (no events yet)")
        else:
            click.echo("Event Store Statistics:")
            click.echo("-" * 40)
            for status_name, count in sorted(stats.items()):
                click.echo(f"  {status_name:15s}  {count}")
        await store.close()

    asyncio.run(_run())


@cli.command()
def dlq() -> None:
    """Show dead-letter queue items."""
    from .router.store import EventStore

    settings = get_settings()

    async def _run():
        store = EventStore(settings.db_path)
        await store.init()
        items = await store.get_dlq()
        if not items:
            click.echo("Dead letter queue is empty ✓")
        else:
            click.echo(f"Dead Letter Queue ({len(items)} items):")
            click.echo("-" * 60)
            for item in items:
                click.echo(f"  [{item['id']}] {item['event_type']}")
                click.echo(f"    error: {item['error']}")
                click.echo(f"    failed_at: {item['failed_at']}")
                click.echo(f"    retries: {item['retries']}")
                click.echo()
        await store.close()

    asyncio.run(_run())


@cli.command()
def seed() -> None:
    """Generate seed data (12 clients, 3 walkers, dogs)."""
    from .db.database import init_database, close_database
    from .db.seed import generate_seed_data

    settings = get_settings()

    async def _run():
        db = await init_database(settings.db_path)
        count = await generate_seed_data(db)
        click.echo(f"✓ Seed data inserted: {count} records")
        await close_database(db)

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
