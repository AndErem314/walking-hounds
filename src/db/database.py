"""Database schema and async connection management.

All business tables (clients, dogs, walkers, walks, invoices, etc.)
are defined here as SQL DDL.  The event_store and dlq tables are
created by EventStore — this module creates the business tables.
"""

from __future__ import annotations

import aiosqlite

_SCHEMA = """
-- ── Clients ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS clients (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    email       TEXT NOT NULL UNIQUE,
    phone       TEXT,
    address     TEXT,
    status      TEXT NOT NULL DEFAULT 'active',   -- active / inactive / pending
    created_at  TEXT NOT NULL
);

-- ── Dogs ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dogs (
    id              TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL REFERENCES clients(id),
    name            TEXT NOT NULL,
    breed           TEXT,
    age_months      INTEGER,                    -- age in months (puppy = 4-10)
    temperament     TEXT,                       -- calm / energetic / anxious / friendly / ...
    sex             TEXT,                       -- male / female
    castrated       TEXT,                       -- neutered / intact (male) / spayed / intact (female)
    in_heat         INTEGER DEFAULT 0,          -- 0 or 1 (female only)
    special_needs   TEXT,
    created_at      TEXT NOT NULL
);

-- ── Walkers ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS walkers (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT,
    email       TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- ── Walk Groups ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS walk_groups (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    walker_id   TEXT NOT NULL REFERENCES walkers(id),
    date        TEXT NOT NULL,                  -- ISO date
    slot        TEXT NOT NULL,                  -- "11:30" etc
    max_dogs    INTEGER NOT NULL DEFAULT 4,
    group_type  TEXT NOT NULL DEFAULT 'standard'  -- standard / puppy
);

-- ── Walks (individual dog bookings) ──────────────────────
CREATE TABLE IF NOT EXISTS walks (
    id          TEXT PRIMARY KEY,
    client_id   TEXT NOT NULL REFERENCES clients(id),
    dog_id      TEXT NOT NULL REFERENCES dogs(id),
    walker_id   TEXT NOT NULL REFERENCES walkers(id),
    group_id    TEXT REFERENCES walk_groups(id),
    date        TEXT NOT NULL,
    slot        TEXT NOT NULL,
    duration    INTEGER NOT NULL DEFAULT 60,
    status      TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled / completed / cancelled
    price_eur   REAL NOT NULL DEFAULT 20.0,
    created_at  TEXT NOT NULL,
    UNIQUE(date, slot, dog_id)
);

CREATE INDEX IF NOT EXISTS idx_walks_date ON walks(date);
CREATE INDEX IF NOT EXISTS idx_walks_walker ON walks(walker_id, date);
CREATE INDEX IF NOT EXISTS idx_walks_status ON walks(status);

-- ── Invoices ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS invoices (
    id              TEXT PRIMARY KEY,
    client_id       TEXT NOT NULL REFERENCES clients(id),
    walk_id         TEXT REFERENCES walks(id),
    amount_eur      REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending / paid / overdue
    due_date        TEXT NOT NULL,
    paid_date       TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_client ON invoices(client_id);

-- ── Messages (communication log) ─────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    client_id   TEXT REFERENCES clients(id),
    direction   TEXT NOT NULL,                  -- inbound / outbound
    channel     TEXT NOT NULL DEFAULT 'email',
    from_email  TEXT,
    to_email    TEXT,
    subject     TEXT,
    body        TEXT,
    sent_at     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'sent'    -- sent / failed / draft
);

CREATE INDEX IF NOT EXISTS idx_messages_client ON messages(client_id);

-- ── Audit Journal ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS journal (
    id                      TEXT PRIMARY KEY,
    event_type              TEXT NOT NULL,
    timestamp               TEXT NOT NULL,
    actor                   TEXT NOT NULL,
    details                 TEXT NOT NULL,      -- JSON
    related_booking_id      TEXT,
    related_client_id       TEXT
);

CREATE INDEX IF NOT EXISTS idx_journal_type ON journal(event_type);
CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(timestamp);

-- ── Approval Gates ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS approval_gates (
    id              TEXT PRIMARY KEY,
    gate_type       TEXT NOT NULL,
    context         TEXT NOT NULL,              -- JSON
    options         TEXT,                       -- JSON array
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending / approved / rejected
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    resolution      TEXT,
    resolver_notes  TEXT,
    originating_event_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_gates_status ON approval_gates(status);

-- ── Onboarding Sessions ──────────────────────────────────
CREATE TABLE IF NOT EXISTS onboarding_sessions (
    id              TEXT PRIMARY KEY,
    email           TEXT NOT NULL,
    client_name     TEXT,
    status          TEXT NOT NULL DEFAULT 'awaiting_info',
    dog_details     TEXT,                       -- JSON (parsed from client reply)
    rate_limit_count INTEGER DEFAULT 0,
    first_contact_at TEXT NOT NULL,
    last_contact_at TEXT NOT NULL,
    resolved_at     TEXT,
    originating_gate_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_onboarding_email ON onboarding_sessions(email);
CREATE INDEX IF NOT EXISTS idx_onboarding_status ON onboarding_sessions(status);

-- ── Processed Emails (dedup) ─────────────────────────────
CREATE TABLE IF NOT EXISTS processed_emails (
    message_id  TEXT PRIMARY KEY,
    received_at TEXT NOT NULL,
    processed_at TEXT NOT NULL
);
"""


async def init_database(db_path: str) -> aiosqlite.Connection:
    """Open the database, enable WAL, and create all tables."""
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript(_SCHEMA)
    await db.commit()
    return db


async def close_database(db: aiosqlite.Connection) -> None:
    await db.close()
