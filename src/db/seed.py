"""Seed data generator — 12 clients, 3 walkers, 14 dogs.

Includes 3 puppies (4-10 months) for the puppy group.
All data is fictional.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import aiosqlite


def _uuid() -> str:
    return uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Walkers ────────────────────────────────────────────────

WALKERS = [
    {"id": _uuid(), "name": "Sarah Klein", "phone": "+49-151-1001-001", "email": "sarah@walking-hounds.local"},
    {"id": _uuid(), "name": "Mike Braun", "phone": "+49-151-1002-002", "email": "mike@walking-hounds.local"},
    {"id": _uuid(), "name": "Emma Wagner", "phone": "+49-151-1003-003", "email": "emma@walking-hounds.local"},
]

# ── Clients + Dogs ─────────────────────────────────────────
# 12 clients, 14 dogs (2 clients have 2 dogs)
# 3 puppies (4-10 months) for puppy group

CLIENTS_DOGS = [
    # --- Adult dogs ---
    {
        "client": {"name": "Lisa Müller", "email": "lisa.mueller@example.com", "phone": "+49-30-1001-1001", "address": "Schönhauser Allee 42, Berlin"},
        "dogs": [
            {"name": "Bello", "breed": "Labrador", "age_months": 36, "temperament": "friendly", "sex": "male", "castrated": "neutered"},
        ],
    },
    {
        "client": {"name": "Tom Schmidt", "email": "tom.schmidt@example.com", "phone": "+49-30-1002-1002", "address": "Kastanienallee 15, Berlin"},
        "dogs": [
            {"name": "Luna", "breed": "Golden Retriever", "age_months": 28, "temperament": "calm", "sex": "female", "castrated": "spayed"},
        ],
    },
    {
        "client": {"name": "Anna Becker", "email": "anna.becker@example.com", "phone": "+49-30-1003-1003", "address": "Bergstrasse 7, Berlin"},
        "dogs": [
            {"name": "Rex", "breed": "German Shepherd", "age_months": 48, "temperament": "energetic", "sex": "male", "castrated": "neutered"},
        ],
    },
    {
        "client": {"name": "Jonas Weber", "email": "jonas.weber@example.com", "phone": "+49-30-1004-1004", "address": "Mauerstrasse 21, Berlin"},
        "dogs": [
            {"name": "Nala", "breed": "Border Collie", "age_months": 30, "temperament": "energetic", "sex": "female", "castrated": "spayed"},
        ],
    },
    {
        "client": {"name": "Mia Hoffmann", "email": "mia.hoffmann@example.com", "phone": "+49-30-1005-1005", "address": "Torstrasse 88, Berlin"},
        "dogs": [
            {"name": "Bruno", "breed": "Boxer", "age_months": 24, "temperament": "friendly", "sex": "male", "castrated": "neutered"},
        ],
    },
    {
        "client": {"name": "Felix Krause", "email": "felix.krause@example.com", "phone": "+49-30-1006-1006", "address": "Linienstrasse 120, Berlin"},
        "dogs": [
            {"name": "Molly", "breed": "Poodle", "age_months": 42, "temperament": "calm", "sex": "female", "castrated": "spayed"},
            {"name": "Charly", "breed": "Poodle", "age_months": 42, "temperament": "anxious", "sex": "male", "castrated": "neutered"},
        ],
    },
    {
        "client": {"name": "Sophie Lange", "email": "sophie.lange@example.com", "phone": "+49-30-1007-1007", "address": "Oranienburger Str. 33, Berlin"},
        "dogs": [
            {"name": "Rocky", "breed": "Bulldog", "age_months": 34, "temperament": "calm", "sex": "male", "castrated": "neutered"},
            {"name": "Gigi", "breed": "French Bulldog", "age_months": 22, "temperament": "friendly", "sex": "female", "castrated": "spayed"},
        ],
    },
    {
        "client": {"name": "David Peters", "email": "david.peters@example.com", "phone": "+49-30-1008-1008", "address": "Friedrichstrasse 55, Berlin"},
        "dogs": [
            {"name": "Bella", "breed": "Beagle", "age_months": 26, "temperament": "friendly", "sex": "female", "castrated": "intact"},
        ],
    },
    {
        "client": {"name": "Julia Richter", "email": "julia.richter@example.com", "phone": "+49-30-1009-1009", "address": "Chausseestrasse 18, Berlin"},
        "dogs": [
            {"name": "Cooper", "breed": "Husky", "age_months": 40, "temperament": "energetic", "sex": "male", "castrated": "neutered"},
        ],
    },
    # --- Puppies (4-10 months) ---
    {
        "client": {"name": "Martin Klein", "email": "martin.klein@example.com", "phone": "+49-30-1010-1010", "address": "Ackerstrasse 76, Berlin"},
        "dogs": [
            {"name": "Milo", "breed": "Labrador Mix", "age_months": 6, "temperament": "friendly", "sex": "male", "castrated": "intact"},
        ],
    },
    {
        "client": {"name": "Nina Fischer", "email": "nina.fischer@example.com", "phone": "+49-30-1011-1011", "address": "Invalidenstrasse 50, Berlin"},
        "dogs": [
            {"name": "Coco", "breed": "Cocker Spaniel", "age_months": 5, "temperament": "energetic", "sex": "female", "castrated": "intact", "in_heat": 0},
        ],
    },
    {
        "client": {"name": "Paul Sommer", "email": "paul.sommer@example.com", "phone": "+49-30-1012-1012", "address": "Zimmerstrasse 12, Berlin"},
        "dogs": [
            {"name": "Zeus", "breed": "Rottweiler Mix", "age_months": 8, "temperament": "friendly", "sex": "male", "castrated": "intact"},
        ],
    },
]


async def generate_seed_data(db: aiosqlite.Connection) -> str:
    """Insert walkers, clients, and dogs. Returns a summary string.
    Idempotent: if data already exists, returns existing counts."""
    now = _now()

    # Check if already seeded
    existing = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM walkers")
    if existing[0]["cnt"] > 0:
        w = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM walkers")
        c = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM clients")
        d = await db.execute_fetchall("SELECT COUNT(*) as cnt FROM dogs")
        return f"{w[0]['cnt'] + c[0]['cnt'] + d[0]['cnt']} records ({w[0]['cnt']} walkers, {c[0]['cnt']} clients, {d[0]['cnt']} dogs) — already seeded"

    count = 0

    # Walkers
    for w in WALKERS:
        await db.execute(
            "INSERT OR IGNORE INTO walkers (id, name, phone, email, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
            (w["id"], w["name"], w["phone"], w["email"], now),
        )
        count += 1

    # Clients + Dogs
    for entry in CLIENTS_DOGS:
        client_id = _uuid()
        c = entry["client"]
        await db.execute(
            """INSERT OR IGNORE INTO clients (id, name, email, phone, address, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'active', ?)""",
            (client_id, c["name"], c["email"], c.get("phone", ""), c.get("address", ""), now),
        )
        count += 1

        for dog in entry["dogs"]:
            dog_id = _uuid()
            await db.execute(
                """INSERT OR IGNORE INTO dogs
                   (id, client_id, name, breed, age_months, temperament, sex, castrated, in_heat, special_needs, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dog_id,
                    client_id,
                    dog["name"],
                    dog.get("breed", ""),
                    dog.get("age_months", 0),
                    dog.get("temperament", ""),
                    dog.get("sex", ""),
                    dog.get("castrated", ""),
                    dog.get("in_heat", 0),
                    dog.get("special_needs", ""),
                    now,
                ),
            )
            count += 1

    await db.commit()
    return f"{count} records ({len(WALKERS)} walkers, {len(CLIENTS_DOGS)} clients, {sum(len(e['dogs']) for e in CLIENTS_DOGS)} dogs)"
