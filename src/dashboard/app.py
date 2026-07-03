"""FastAPI Dashboard for Walking Hounds.

Endpoints:
  GET  /                         — Dashboard home (today's schedule + stats)
  GET  /schedule                 — Full schedule view
  GET  /schedule?date=YYYY-MM-DD — Schedule for specific date
  GET  /journal                  — Audit journal (recent entries)
  GET  /invoices                 — Invoice list
  GET  /approvals                — Pending approval gates
  POST /approvals/{id}/resolve   — Resolve an approval gate (HTMX)
  POST /invoices/{id}/mark-paid  — Mark invoice as paid (HTMX)
  GET  /agents                   — Agent health dashboard
  WS   /ws                       — WebSocket for live updates
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..config import Settings, get_settings
from ..router.event import (
    HumanApproved,
    HumanRejected,
    PaymentConfirmed,
)
from ..router.router import EventRouter

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = _BASE_DIR / "templates"
STATIC_DIR = _BASE_DIR / "static"


def create_dashboard_app(
    router: EventRouter,
    settings: Settings | None = None,
) -> FastAPI:
    """Create the FastAPI dashboard application."""
    app = FastAPI(title="Walking Hounds Dashboard", docs_url=None, redoc_url=None)
    settings = settings or get_settings()

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # WebSocket connection manager for live updates
    ws_clients: set[WebSocket] = set()

    async def broadcast(message: dict) -> None:
        """Send a message to all connected WebSocket clients."""
        dead = set()
        for ws in ws_clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.add(ws)
        ws_clients.difference_update(dead)

    @property_db_helper(router)
    def get_db():
        ...

    # ── Routes ──────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard_home(request: Request, date: str | None = None):
        from datetime import timedelta

        db = router.store.db
        target_date = date or datetime.now(timezone.utc).date().isoformat()
        today = datetime.now(timezone.utc).date().isoformat()

        # Calculate prev/next for schedule navigation
        target_dt = datetime.fromisoformat(target_date)
        prev_date = (target_dt - timedelta(days=1)).date().isoformat()
        next_date = (target_dt + timedelta(days=1)).date().isoformat()

        # Walks for the target date
        walks = await _get_walks_for_date(db, target_date)

        # Stats
        stats = await _get_dashboard_stats(db)

        # Pending approvals
        approvals = await _get_pending_approvals(db)

        # Recent journal entries
        journal = await _get_recent_journal(db, limit=10)

        # Pending invoices
        invoices = await _get_pending_invoices(db)

        return templates.TemplateResponse(request=request, name="dashboard.html", context={
            "request": request,
            "today": today,
            "target_date": target_date,
            "prev_date": prev_date,
            "next_date": next_date,
            "walks": walks,
            "stats": stats,
            "approvals": approvals,
            "journal": journal,
            "invoices": invoices,
        })

    @app.get("/schedule", response_class=HTMLResponse)
    async def schedule_view(request: Request, date: str | None = None):
        from datetime import timedelta

        db = router.store.db
        target_date = date or datetime.now(timezone.utc).date().isoformat()

        # Compute the Monday of the week containing target_date
        target_dt = datetime.fromisoformat(target_date)
        monday = target_dt - timedelta(days=target_dt.weekday())  # weekday() → Mon=0

        # Week boundaries: Monday through Friday
        week_days: list[str] = []
        weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri"]
        for i in range(5):
            d = monday + timedelta(days=i)
            week_days.append(d.date().isoformat())

        # Fetch all walks for the week
        week_walks = await _get_walks_for_week(db, week_days[0], week_days[-1])

        # Build the grid: slot × day → list of walks
        slots = settings.walk_slot_list
        grid: dict[str, dict[str, list[dict]]] = {}
        for slot in slots:
            grid[slot] = {day: [] for day in week_days}
        for w in week_walks:
            slot = w.get("slot", "")
            day = w.get("date", "")
            if slot in grid and day in grid[slot]:
                grid[slot][day].append(w)
            else:
                # Fallback: walks outside defined slots appear under their slot
                if slot:
                    grid.setdefault(slot, {day: [] for day in week_days})
                    if day in grid[slot]:
                        grid[slot][day].append(w)

        # Navigation: prev/next week
        prev_monday = (monday - timedelta(days=7)).date().isoformat()
        next_monday = (monday + timedelta(days=7)).date().isoformat()
        today = datetime.now(timezone.utc).date().isoformat()

        return templates.TemplateResponse(request=request, name="schedule.html", context={
            "request": request,
            "target_date": target_date,
            "prev_date": prev_monday,
            "next_date": next_monday,
            "today": today,
            "week_days": week_days,
            "weekday_labels": weekday_labels,
            "slots": slots,
            "grid": grid,
        })

    @app.get("/journal", response_class=HTMLResponse)
    async def journal_view(request: Request, limit: int = 100):
        db = router.store.db
        entries = await _get_recent_journal(db, limit=limit)
        return templates.TemplateResponse(request=request, name="journal.html", context={
            "request": request,
            "entries": entries,
        })

    @app.get("/invoices", response_class=HTMLResponse)
    async def invoices_view(request: Request):
        db = router.store.db
        pending = await _get_pending_invoices(db)
        paid = await _get_paid_invoices(db)
        return templates.TemplateResponse(request=request, name="invoices.html", context={
            "request": request,
            "pending": pending,
            "paid": paid,
        })

    @app.get("/approvals", response_class=HTMLResponse)
    async def approvals_view(request: Request):
        db = router.store.db
        pending = await _get_pending_approvals(db)
        resolved = await _get_resolved_approvals(db, limit=20)
        return templates.TemplateResponse(request=request, name="approvals.html", context={
            "request": request,
            "pending": pending,
            "resolved": resolved,
        })

    @app.post("/approvals/{gate_id}/resolve")
    async def resolve_approval(gate_id: str, decision: str = "approved", notes: str = ""):
        db = router.store.db

        # Look up the gate
        rows = await db.execute_fetchall(
            "SELECT * FROM approval_gates WHERE id = ? AND status = 'pending'",
            (gate_id,),
        )
        if not rows:
            return JSONResponse({"error": "Gate not found or already resolved"}, status_code=404)

        gate = dict(rows[0])
        now = datetime.now(timezone.utc).isoformat()

        # Update the gate
        await db.execute(
            "UPDATE approval_gates SET status = ?, resolved_at = ?, resolution = ?, resolver_notes = ? WHERE id = ?",
            (decision, now, decision, notes, gate_id),
        )
        await db.commit()

        # Emit the appropriate event
        if decision == "approved":
            await router.publish(HumanApproved(
                gate_id=gate_id,
                decision="approved",
                notes=notes,
            ))
        else:
            await router.publish(HumanRejected(
                gate_id=gate_id,
                reason=notes,
            ))

        # Broadcast update via WebSocket
        await broadcast({"type": "approval_resolved", "gate_id": gate_id, "decision": decision})

        # Return HTMX partial (updated pending list)
        pending = await _get_pending_approvals(db)
        # For HTMX partials, render directly from the env
        template = templates.env.get_template("partials/approval_list.html")
        html = template.render(pending=pending)
        from starlette.responses import HTMLResponse as _HR
        return _HR(html)

    @app.post("/invoices/{invoice_id}/mark-paid")
    async def mark_invoice_paid(invoice_id: str):
        db = router.store.db

        # Look up the invoice
        rows = await db.execute_fetchall(
            "SELECT * FROM invoices WHERE id = ? AND status = 'pending'",
            (invoice_id,),
        )
        if not rows:
            return JSONResponse({"error": "Invoice not found or already paid"}, status_code=404)

        invoice = dict(rows[0])

        # Look up client email
        client_rows = await db.execute_fetchall(
            "SELECT email FROM clients WHERE id = ?",
            (invoice["client_id"],),
        )
        client_email = client_rows[0]["email"] if client_rows else ""

        # Mark invoice as paid directly (human action = dashboard is the gate)
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "UPDATE invoices SET status = 'paid', paid_date = ? WHERE id = ?",
            (now, invoice_id),
        )
        await db.commit()

        # Also emit PaymentConfirmed event for the audit trail
        await router.publish(PaymentConfirmed(
            invoice_id=invoice_id,
            client_email=client_email,
            amount_eur=invoice["amount_eur"],
        ))

        # Broadcast update
        await broadcast({"type": "invoice_paid", "invoice_id": invoice_id})

        # Return HTMX partial (updated pending list)
        pending = await _get_pending_invoices(db)
        template = templates.env.get_template("partials/invoice_list.html")
        html = template.render(pending=pending)
        from starlette.responses import HTMLResponse as _HR
        return _HR(html)

    @app.get("/agents", response_class=HTMLResponse)
    async def agents_view(request: Request):
        db = router.store.db

        # Event store stats
        event_stats = await router.store.get_stats()

        # Journal count
        journal_rows = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM journal")
        journal_count = journal_rows[0]["cnt"] if journal_rows else 0

        # DLQ count
        dlq_rows = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM dlq")
        dlq_count = dlq_rows[0]["cnt"] if dlq_rows else 0

        return templates.TemplateResponse(request=request, name="agents.html", context={
            "request": request,
            "event_stats": event_stats,
            "journal_count": journal_count,
            "dlq_count": dlq_count,
        })

    @app.get("/api/stats")
    async def api_stats():
        db = router.store.db
        stats = await _get_dashboard_stats(db)
        return JSONResponse(stats)

    @app.get("/api/journal")
    async def api_journal(limit: int = 50):
        db = router.store.db
        entries = await _get_recent_journal(db, limit=limit)
        return JSONResponse(entries)

    @app.get("/api/approvals")
    async def api_approvals():
        db = router.store.db
        pending = await _get_pending_approvals(db)
        return JSONResponse(pending)

    @app.get("/api/walks")
    async def api_walks(date: str | None = None):
        db = router.store.db
        target_date = date or datetime.now(timezone.utc).date().isoformat()
        walks = await _get_walks_for_date(db, target_date)
        return JSONResponse(walks)

    # ── WebSocket ───────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        ws_clients.add(websocket)
        try:
            while True:
                # Keep connection alive, listen for client pings
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            ws_clients.discard(websocket)
        except Exception:
            ws_clients.discard(websocket)

    # Expose broadcast function for the main loop to call
    app.state.broadcast = broadcast
    app.state.ws_clients = ws_clients

    return app


# ── Database Query Helpers ──────────────────────────────────

async def _get_walks_for_date(db: aiosqlite.Connection, date_str: str) -> list[dict]:
    rows = await db.execute_fetchall(
        """SELECT w.*, c.name as client_name, c.email as client_email,
                  d.name as dog_name, d.breed, d.age_months, d.temperament,
                  d.sex, d.castrated, d.in_heat, d.special_needs,
                  wl.name as walker_name,
                  g.name as group_name, g.group_type
           FROM walks w
           JOIN clients c ON w.client_id = c.id
           JOIN dogs d ON w.dog_id = d.id
           JOIN walkers wl ON w.walker_id = wl.id
           LEFT JOIN walk_groups g ON w.group_id = g.id
           WHERE w.date = ?
           ORDER BY w.slot, w.created_at""",
        (date_str,),
    )
    return [dict(r) for r in rows]


async def _get_walks_for_week(db: aiosqlite.Connection, monday: str, friday: str) -> list[dict]:
    """Fetch all scheduled walks for a week (Mon–Fri)."""
    rows = await db.execute_fetchall(
        """SELECT w.*, c.name as client_name, c.email as client_email,
                  d.name as dog_name, d.breed, d.age_months, d.temperament,
                  d.sex, d.castrated, d.in_heat, d.special_needs,
                  wl.name as walker_name,
                  g.name as group_name, g.group_type
           FROM walks w
           JOIN clients c ON w.client_id = c.id
           JOIN dogs d ON w.dog_id = d.id
           JOIN walkers wl ON w.walker_id = wl.id
           LEFT JOIN walk_groups g ON w.group_id = g.id
           WHERE w.date >= ? AND w.date <= ? AND w.status = 'scheduled'
           ORDER BY w.date, w.slot, w.created_at""",
        (monday, friday),
    )
    return [dict(r) for r in rows]


async def _get_dashboard_stats(db: aiosqlite.Connection) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()

    # Walks today
    walk_rows = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM walks WHERE date = ? AND status = 'scheduled'",
        (today,),
    )
    walks_today = walk_rows[0]["cnt"] if walk_rows else 0

    # Pending invoices
    inv_rows = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt, COALESCE(SUM(amount_eur), 0) as total FROM invoices WHERE status = 'pending'"
    )
    pending_invoices = inv_rows[0]["cnt"] if inv_rows else 0
    pending_total = inv_rows[0]["total"] if inv_rows else 0

    # Pending approvals
    appr_rows = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM approval_gates WHERE status = 'pending'"
    )
    pending_approvals = appr_rows[0]["cnt"] if appr_rows else 0

    # Completed walks (all time)
    comp_rows = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM walks WHERE status = 'completed'"
    )
    completed_walks = comp_rows[0]["cnt"] if comp_rows else 0

    # Active clients
    client_rows = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM clients WHERE status = 'active'"
    )
    active_clients = client_rows[0]["cnt"] if client_rows else 0

    # Total dogs
    dog_rows = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM dogs"
    )
    total_dogs = dog_rows[0]["cnt"] if dog_rows else 0

    # Active walkers
    walker_rows = await db.execute_fetchall(
        "SELECT COUNT(*) as cnt FROM walkers WHERE active = 1"
    )
    active_walkers = walker_rows[0]["cnt"] if walker_rows else 0

    return {
        "walks_today": walks_today,
        "pending_invoices": pending_invoices,
        "pending_total_eur": round(pending_total, 2),
        "pending_approvals": pending_approvals,
        "completed_walks": completed_walks,
        "active_clients": active_clients,
        "total_dogs": total_dogs,
        "active_walkers": active_walkers,
    }


async def _get_pending_approvals(db: aiosqlite.Connection) -> list[dict]:
    rows = await db.execute_fetchall(
        """SELECT * FROM approval_gates WHERE status = 'pending' ORDER BY created_at DESC"""
    )
    results = []
    for r in rows:
        gate = dict(r)
        try:
            gate["context"] = json.loads(gate.get("context") or "{}")
        except (json.JSONDecodeError, TypeError):
            gate["context"] = {}
        try:
            gate["options"] = json.loads(gate.get("options") or "[]")
        except (json.JSONDecodeError, TypeError):
            gate["options"] = []
        results.append(gate)
    return results


async def _get_resolved_approvals(db: aiosqlite.Connection, limit: int = 20) -> list[dict]:
    rows = await db.execute_fetchall(
        """SELECT * FROM approval_gates WHERE status != 'pending' ORDER BY resolved_at DESC LIMIT ?""",
        (limit,),
    )
    results = []
    for r in rows:
        gate = dict(r)
        try:
            gate["context"] = json.loads(gate.get("context") or "{}")
        except (json.JSONDecodeError, TypeError):
            gate["context"] = {}
        results.append(gate)
    return results


async def _get_pending_invoices(db: aiosqlite.Connection) -> list[dict]:
    rows = await db.execute_fetchall(
        """SELECT i.*, c.name as client_name, c.email as client_email
           FROM invoices i
           JOIN clients c ON i.client_id = c.id
           WHERE i.status = 'pending'
           ORDER BY i.created_at DESC"""
    )
    return [dict(r) for r in rows]


async def _get_paid_invoices(db: aiosqlite.Connection) -> list[dict]:
    rows = await db.execute_fetchall(
        """SELECT i.*, c.name as client_name, c.email as client_email
           FROM invoices i
           JOIN clients c ON i.client_id = c.id
           WHERE i.status = 'paid'
           ORDER BY i.paid_date DESC LIMIT 50"""
    )
    return [dict(r) for r in rows]


async def _get_recent_journal(db: aiosqlite.Connection, limit: int = 50) -> list[dict]:
    rows = await db.execute_fetchall(
        """SELECT j.* FROM journal j ORDER BY j.timestamp DESC LIMIT ?""",
        (limit,),
    )
    results = []
    for r in rows:
        entry = dict(r)
        try:
            entry["details"] = json.loads(entry.get("details") or "{}")
        except (json.JSONDecodeError, TypeError):
            entry["details"] = {}
        results.append(entry)
    return results


# Dummy decorator to satisfy the type checker — no-op
def property_db_helper(router):
    def decorator(fn):
        return fn
    return decorator
