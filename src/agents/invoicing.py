"""Invoicing Agent — generates invoices, tracks payments, escalates.

Subscribes to: ScheduleConfirmed, CancellationConfirmed, WalkCompleted, PaymentConfirmed
Emits: InvoiceGenerated, PaymentReminder, HumanApprovalRequired

Business rules:
- Invoice generated immediately after booking confirmation (€20/walk)
- Late cancellation (< 24h) → 50% invoice (adjusted)
- Human marks payment as paid via dashboard → PaymentConfirmed event
- 1st reminder: polite (7 days after invoice)
- 2nd reminder: firm (14 days after invoice)
- After 2nd reminder unpaid → escalate to human
- NEVER processes refunds or adjusts prices (human only)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from typing import Any

import aiosqlite

from ..router.event import (
    BaseEvent,
    CancellationConfirmed,
    HumanApprovalRequired,
    InvoiceGenerated,
    PaymentConfirmed,
    PaymentReminder,
    ScheduleConfirmed,
    WalkCompleted,
)
from ..router.router import EventRouter
from ..config import Settings, get_settings
from .base import BaseAgent

logger = logging.getLogger(__name__)


def _uuid() -> str:
    return uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InvoicingAgent(BaseAgent):
    """Manages billing cycle, invoice generation, and payment tracking."""

    name = "InvoicingAgent"

    def __init__(self, router: EventRouter, settings: Settings | None = None):
        super().__init__(router)
        self._settings = settings or get_settings()
        self._db: aiosqlite.Connection | None = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            self._db = self._router.store.db
        return self._db

    def subscribed_event_types(self) -> list[str]:
        return [
            "ScheduleConfirmed",
            "CancellationConfirmed",
            "WalkCompleted",
            "PaymentConfirmed",
        ]

    async def handle(self, event: BaseEvent) -> None:
        if isinstance(event, ScheduleConfirmed):
            await self._handle_booking(event)
        elif isinstance(event, CancellationConfirmed):
            await self._handle_cancellation(event)
        elif isinstance(event, WalkCompleted):
            await self._handle_walk_completed(event)
        elif isinstance(event, PaymentConfirmed):
            await self._handle_payment(event)

    # ── Invoice Generation ──────────────────────────────────

    async def _handle_booking(self, event: ScheduleConfirmed) -> None:
        """Generate an invoice immediately after booking confirmation."""
        # Look up the walk to get client_id
        walk = await self._get_walk(event.booking_id)
        if not walk:
            logger.warning("InvoicingAgent: walk %s not found", event.booking_id)
            return

        invoice_id = _uuid()
        now = _now()
        due_date = (now + timedelta(days=7)).date().isoformat()
        price = self._settings.walk_price_eur

        await self.db.execute(
            """INSERT INTO invoices (id, client_id, walk_id, amount_eur, status, due_date, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
            (invoice_id, walk["client_id"], event.booking_id, price, due_date, now.isoformat()),
        )
        await self.db.commit()

        logger.info(
            "InvoicingAgent: invoice %s created for walk %s (€%.2f)",
            invoice_id, event.booking_id, price,
        )

        await self.emit(InvoiceGenerated(
            invoice_id=invoice_id,
            client_email=event.client_email,
            client_name=event.client_name,
            amount_eur=price,
            due_date=due_date,
            walk_date=event.walk_date,
            booking_id=event.booking_id,
        ))

    async def _handle_cancellation(self, event: CancellationConfirmed) -> None:
        """Adjust invoice for late cancellation (50% charge)."""
        walk = await self._get_walk(event.booking_id)
        if not walk:
            return

        # Find the invoice for this walk
        rows = await self.db.execute_fetchall(
            "SELECT * FROM invoices WHERE walk_id = ?", (event.booking_id,)
        )
        if not rows:
            return

        invoice = dict(rows[0])

        if event.late_cancellation:
            # Late cancel → 50% charge, update invoice amount
            new_amount = self._settings.walk_price_eur * 0.5
            await self.db.execute(
                "UPDATE invoices SET amount_eur = ? WHERE id = ?",
                (new_amount, invoice["id"]),
            )
            await self.db.commit()

            logger.info(
                "InvoicingAgent: invoice %s adjusted to €%.2f (late cancellation)",
                invoice["id"], new_amount,
            )

            await self.emit(InvoiceGenerated(
                invoice_id=invoice["id"],
                client_email=event.client_email,
                client_name=event.client_name,
                amount_eur=new_amount,
                due_date=invoice["due_date"],
                walk_date="",
                booking_id=event.booking_id,
            ))
        else:
            # Full refund → mark invoice as paid with 0 amount or cancel it
            await self.db.execute(
                "UPDATE invoices SET amount_eur = 0, status = 'paid', paid_date = ? WHERE id = ?",
                (_now().isoformat(), invoice["id"]),
            )
            await self.db.commit()

            logger.info(
                "InvoicingAgent: invoice %s zeroed (full refund, > 24h cancel)",
                invoice["id"],
            )

    async def _handle_walk_completed(self, event: WalkCompleted) -> None:
        """Mark the associated invoice as finalised (walk completed)."""
        # The invoice was already created at booking time.
        # On walk completion, we just ensure the invoice is still pending.
        # If no invoice exists (edge case), create one.
        rows = await self.db.execute_fetchall(
            "SELECT * FROM invoices WHERE walk_id = ?", (event.booking_id,)
        )
        if not rows:
            walk = await self._get_walk(event.booking_id)
            if walk:
                invoice_id = _uuid()
                now = _now()
                due_date = (now + timedelta(days=7)).date().isoformat()
                await self.db.execute(
                    """INSERT INTO invoices (id, client_id, walk_id, amount_eur, status, due_date, created_at)
                       VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                    (invoice_id, walk["client_id"], event.booking_id,
                     self._settings.walk_price_eur, due_date, now.isoformat()),
                )
                await self.db.commit()
                logger.info("InvoicingAgent: late invoice %s created for completed walk", invoice_id)

    async def _handle_payment(self, event: PaymentConfirmed) -> None:
        """Human marked payment as received via dashboard."""
        now = _now().isoformat()
        await self.db.execute(
            "UPDATE invoices SET status = 'paid', paid_date = ? WHERE id = ?",
            (now, event.invoice_id),
        )
        await self.db.commit()

        logger.info(
            "InvoicingAgent: invoice %s marked as paid (€%.2f)",
            event.invoice_id, event.amount_eur,
        )

    # ── Payment Reminder Check ──────────────────────────────

    async def check_overdue_invoices(self) -> list[dict]:
        """Check for overdue invoices and emit reminders.

        Called periodically by the Reminder Agent's timer loop.
        Returns list of overdue invoice dicts for logging.
        """
        now = _now()
        overdue: list[dict] = []

        # Find all pending invoices
        rows = await self.db.execute_fetchall(
            "SELECT * FROM invoices WHERE status = 'pending' ORDER BY created_at"
        )

        for row in rows:
            invoice = dict(row)
            created = self._parse_dt(invoice.get("created_at"))
            if not created:
                continue

            days_since = (now - created).days

            if days_since >= 14 and invoice["amount_eur"] > 0:
                # 2nd reminder — escalate to human
                client = await self._get_client(invoice["client_id"])
                if client:
                    await self.emit(PaymentReminder(
                        invoice_id=invoice["id"],
                        client_email=client["email"],
                        client_name=client["name"],
                        reminder_count=2,
                        amount_eur=invoice["amount_eur"],
                    ))

                    await self.emit(HumanApprovalRequired(
                        gate_type="payment_escalation",
                        context={
                            "invoice_id": invoice["id"],
                            "client_name": client["name"],
                            "client_email": client["email"],
                            "amount_eur": invoice["amount_eur"],
                            "days_overdue": days_since,
                        },
                        options=["contact_client", "waive_fee", "extend_deadline"],
                    ))

                    logger.warning(
                        "InvoicingAgent: invoice %s overdue %dd → human escalation",
                        invoice["id"], days_since,
                    )
                    overdue.append(invoice)

            elif days_since >= 7 and invoice["amount_eur"] > 0:
                # 1st reminder
                client = await self._get_client(invoice["client_id"])
                if client:
                    await self.emit(PaymentReminder(
                        invoice_id=invoice["id"],
                        client_email=client["email"],
                        client_name=client["name"],
                        reminder_count=1,
                        amount_eur=invoice["amount_eur"],
                    ))

                    logger.info(
                        "InvoicingAgent: 1st reminder for invoice %s (€%.2f, %dd overdue)",
                        invoice["id"], invoice["amount_eur"], days_since,
                    )
                    overdue.append(invoice)

        return overdue

    # ── Helpers ─────────────────────────────────────────────

    async def _get_walk(self, walk_id: str) -> dict | None:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM walks WHERE id = ?", (walk_id,)
        )
        return dict(rows[0]) if rows else None

    async def _get_client(self, client_id: str) -> dict | None:
        rows = await self.db.execute_fetchall(
            "SELECT * FROM clients WHERE id = ?", (client_id,)
        )
        return dict(rows[0]) if rows else None

    @staticmethod
    def _parse_dt(dt_str: str | None) -> datetime | None:
        if not dt_str:
            return None
        try:
            return datetime.fromisoformat(dt_str)
        except (ValueError, TypeError):
            return None

    async def get_invoice_stats(self) -> dict:
        """Return invoice statistics for dashboard."""
        rows = await self.db.execute_fetchall(
            "SELECT status, COUNT(*) as cnt, SUM(amount_eur) as total FROM invoices GROUP BY status"
        )
        return {r["status"]: {"count": r["cnt"], "total": r["total"] or 0} for r in rows}
