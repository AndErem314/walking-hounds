"""Tests for the FastAPI Dashboard."""

import asyncio
import json
import pytest
from datetime import datetime, timezone

from httpx import AsyncClient, ASGITransport
from src.router.event import (
    BookingIntent,
    ScheduleConfirmed,
    HumanApprovalRequired,
    InvoiceGenerated,
    PaymentConfirmed,
)
from src.router.store import EventStore
from src.router.router import EventRouter
from src.dashboard.app import create_dashboard_app
from src.db.database import init_database, close_database
from src.db.seed import generate_seed_data
from src.config import Settings


@pytest.fixture
async def app_and_router(tmp_path):
    """Create a test dashboard app with seeded data."""
    db_path = str(tmp_path / "test_dashboard.db")
    db = await init_database(db_path)
    await generate_seed_data(db)

    store = EventStore(db_path)
    await store.init()
    router = EventRouter(store)

    settings = Settings(
        db_path=db_path,
        imap_host="",
        imap_user="",
        imap_password="",
        smtp_host="",
        smtp_user="",
        smtp_password="",
    )

    app = create_dashboard_app(router, settings)

    # Start the router so it can handle events
    await router.start()

    yield app, router, db

    await router.stop()
    await store.close()
    await close_database(db)


class TestDashboardRoutes:

    async def test_home_page(self, app_and_router):
        """Dashboard home should return HTML."""
        app, router, db = app_and_router
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/")
        assert resp.status_code == 200
        assert "Walking Hounds" in resp.text
        assert "Walks Today" in resp.text

    async def test_schedule_page(self, app_and_router):
        """Schedule page should return HTML."""
        app, router, db = app_and_router
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/schedule")
        assert resp.status_code == 200
        assert "Schedule" in resp.text

    async def test_journal_page(self, app_and_router):
        """Journal page should return HTML."""
        app, router, db = app_and_router
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/journal")
        assert resp.status_code == 200
        assert "Journal" in resp.text or "journal" in resp.text.lower()

    async def test_invoices_page(self, app_and_router):
        """Invoices page should return HTML."""
        app, router, db = app_and_router
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/invoices")
        assert resp.status_code == 200
        assert "Invoice" in resp.text

    async def test_approvals_page(self, app_and_router):
        """Approvals page should return HTML."""
        app, router, db = app_and_router
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/approvals")
        assert resp.status_code == 200
        assert "Approval" in resp.text

    async def test_agents_page(self, app_and_router):
        """System/agents page should return HTML."""
        app, router, db = app_and_router
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/agents")
        assert resp.status_code == 200
        assert "System" in resp.text or "Agent" in resp.text

    async def test_api_stats(self, app_and_router):
        """API /api/stats should return JSON stats."""
        app, router, db = app_and_router
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "walks_today" in data
        assert "pending_invoices" in data
        assert "pending_approvals" in data
        assert "active_clients" in data
        assert "total_dogs" in data
        assert "active_walkers" in data

    async def test_api_journal_empty(self, app_and_router):
        """API /api/journal should return empty list initially."""
        app, router, db = app_and_router
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/journal")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_api_walks(self, app_and_router):
        """API /api/walks should return walks for a date."""
        app, router, db = app_and_router

        # Insert a walk directly
        from uuid import uuid4
        now = datetime.now(timezone.utc).isoformat()
        today = datetime.now(timezone.utc).date().isoformat()

        # Get client, dog, walker from seed
        clients = await db.execute_fetchall("SELECT * FROM clients LIMIT 1")
        dogs = await db.execute_fetchall("SELECT * FROM dogs LIMIT 1")
        walkers = await db.execute_fetchall("SELECT * FROM walkers LIMIT 1")

        if clients and dogs and walkers:
            walk_id = uuid4().hex
            await db.execute(
                """INSERT INTO walks (id, client_id, dog_id, walker_id, date, slot, duration, status, price_eur, created_at)
                   VALUES (?, ?, ?, ?, ?, '11:30', 60, 'scheduled', 20.0, ?)""",
                (walk_id, clients[0]["id"], dogs[0]["id"], walkers[0]["id"], today, now),
            )
            await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/walks?date={today}")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["slot"] == "11:30"


class TestDashboardApprovals:

    async def test_resolve_approval_approve(self, app_and_router):
        """Approving a gate should update status and emit HumanApproved."""
        app, router, db = app_and_router

        # Create a test approval gate
        from uuid import uuid4
        gate_id = uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO approval_gates (id, gate_type, context, options, status, created_at)
               VALUES (?, 'complaint_response', ?, '["approve","reject"]', 'pending', ?)""",
            (gate_id, json.dumps({"client_email": "test@example.com"}), now),
        )
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/approvals/{gate_id}/resolve?decision=approved&notes=Looks good")
        assert resp.status_code == 200

        # Verify the gate was updated
        rows = await db.execute_fetchall("SELECT * FROM approval_gates WHERE id = ?", (gate_id,))
        assert len(rows) == 1
        assert dict(rows[0])["status"] == "approved"
        assert dict(rows[0])["resolver_notes"] == "Looks good"

    async def test_resolve_approval_reject(self, app_and_router):
        """Rejecting a gate should update status and emit HumanRejected."""
        app, router, db = app_and_router

        from uuid import uuid4
        gate_id = uuid4().hex
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO approval_gates (id, gate_type, context, options, status, created_at)
               VALUES (?, 'schedule_conflict', ?, '["approve","reject"]', 'pending', ?)""",
            (gate_id, json.dumps({"details": "No walker available"}), now),
        )
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/approvals/{gate_id}/resolve?decision=rejected&notes=No good")
        assert resp.status_code == 200

        rows = await db.execute_fetchall("SELECT * FROM approval_gates WHERE id = ?", (gate_id,))
        assert dict(rows[0])["status"] == "rejected"

    async def test_resolve_nonexistent_gate(self, app_and_router):
        """Resolving a nonexistent gate should return 404."""
        app, router, db = app_and_router
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/approvals/nonexistent/resolve?decision=approved")
        assert resp.status_code == 404


class TestDashboardInvoices:

    async def test_mark_invoice_paid(self, app_and_router):
        """Marking an invoice as paid should emit PaymentConfirmed and update status."""
        app, router, db = app_and_router

        from uuid import uuid4
        now = datetime.now(timezone.utc).isoformat()
        clients = await db.execute_fetchall("SELECT * FROM clients LIMIT 1")
        assert clients
        client = dict(clients[0])

        invoice_id = uuid4().hex
        await db.execute(
            """INSERT INTO invoices (id, client_id, amount_eur, status, due_date, created_at)
               VALUES (?, ?, 20.0, 'pending', '2026-07-22', ?)""",
            (invoice_id, client["id"], now),
        )
        await db.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client_http:
            resp = await client_http.post(f"/invoices/{invoice_id}/mark-paid")
        assert resp.status_code == 200

        # Give the event time to process
        await asyncio.sleep(0.3)

        rows = await db.execute_fetchall("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
        assert dict(rows[0])["status"] == "paid"
        assert dict(rows[0])["paid_date"] is not None

    async def test_mark_nonexistent_invoice(self, app_and_router):
        """Marking a nonexistent invoice should return 404."""
        app, router, db = app_and_router
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/invoices/nonexistent/mark-paid")
        assert resp.status_code == 404


class TestDashboardStatsWithSeed:

    async def test_stats_with_seed_data(self, app_and_router):
        """Stats should reflect seed data (12 clients, 3 walkers, 14 dogs)."""
        app, router, db = app_and_router
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/stats")
        data = resp.json()
        assert data["active_clients"] >= 12
        assert data["active_walkers"] == 3
        assert data["total_dogs"] >= 14
