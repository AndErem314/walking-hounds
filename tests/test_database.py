"""Tests for database schema and seed data."""

from __future__ import annotations

import pytest

from src.db.database import init_database, close_database
from src.db.models import fetch_all, fetch_one, execute, journal_entry
from src.db.seed import generate_seed_data, WALKERS, CLIENTS_DOGS


@pytest.fixture
async def db(tmp_db_path):
    conn = await init_database(tmp_db_path)
    yield conn
    await close_database(conn)


class TestDatabaseSchema:
    async def test_all_tables_created(self, db):
        rows = await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        table_names = {r["name"] for r in rows}
        # Business tables created by init_database
        business_tables = {
            "clients", "dogs", "walkers", "walk_groups", "walks",
            "invoices", "messages", "journal", "approval_gates",
            "processed_emails",
        }
        assert business_tables.issubset(table_names), f"Missing: {business_tables - table_names}"

    async def test_wal_mode(self, db):
        rows = await db.execute_fetchall("PRAGMA journal_mode")
        assert rows[0][0].lower() == "wal"

    async def test_foreign_keys_on(self, db):
        rows = await db.execute_fetchall("PRAGMA foreign_keys")
        assert rows[0][0] == 1


class TestDatabaseHelpers:
    async def test_fetch_all_returns_dicts(self, db):
        # Insert a walker
        await execute(
            db,
            "INSERT INTO walkers (id, name, active, created_at) VALUES (?, ?, 1, '2025-01-01')",
            ("w1", "Test Walker"),
        )
        results = await fetch_all(db, "SELECT * FROM walkers")
        assert len(results) == 1
        assert results[0]["name"] == "Test Walker"
        assert isinstance(results[0], dict)

    async def test_fetch_one_returns_single_or_none(self, db):
        await execute(
            db,
            "INSERT INTO walkers (id, name, active, created_at) VALUES (?, ?, 1, '2025-01-01')",
            ("w1", "Test Walker"),
        )
        result = await fetch_one(db, "SELECT * FROM walkers WHERE id=?", ("w1",))
        assert result is not None
        assert result["name"] == "Test Walker"

        result_none = await fetch_one(db, "SELECT * FROM walkers WHERE id=?", ("nonexistent",))
        assert result_none is None

    async def test_journal_entry(self, db):
        await journal_entry(
            db,
            event_type="TestEvent",
            actor="test",
            details={"key": "value"},
        )
        rows = await fetch_all(db, "SELECT * FROM journal")
        assert len(rows) == 1
        assert rows[0]["event_type"] == "TestEvent"
        import json
        details = json.loads(rows[0]["details"])
        assert details["key"] == "value"


class TestSeedData:
    async def test_seed_inserts_expected_counts(self, db):
        summary = await generate_seed_data(db)
        assert "3 walkers" in summary
        assert "12 clients" in summary
        assert "14 dogs" in summary

    async def test_seed_walkers(self, db):
        await generate_seed_data(db)
        walkers = await fetch_all(db, "SELECT * FROM walkers WHERE active=1")
        assert len(walkers) == 3
        names = {w["name"] for w in walkers}
        assert "Sarah Klein" in names
        assert "Mike Braun" in names
        assert "Emma Wagner" in names

    async def test_seed_clients(self, db):
        await generate_seed_data(db)
        clients = await fetch_all(db, "SELECT * FROM clients WHERE status='active'")
        assert len(clients) == 12

    async def test_seed_dogs(self, db):
        await generate_seed_data(db)
        dogs = await fetch_all(db, "SELECT * FROM dogs")
        assert len(dogs) == 14

    async def test_seed_has_puppies(self, db):
        """3 dogs should be in the 4-10 month puppy range."""
        await generate_seed_data(db)
        puppies = await fetch_all(
            db,
            "SELECT * FROM dogs WHERE age_months BETWEEN 4 AND 10"
        )
        assert len(puppies) == 3
        puppy_names = {d["name"] for d in puppies}
        assert "Milo" in puppy_names
        assert "Coco" in puppy_names
        assert "Zeus" in puppy_names

    async def test_seed_dog_info_card_fields(self, db):
        """Verify dog records have breed, age, temperament, sex, castrated."""
        await generate_seed_data(db)
        dogs = await fetch_all(db, "SELECT * FROM dogs")
        for dog in dogs:
            assert dog["breed"] != ""
            assert dog["age_months"] > 0
            assert dog["temperament"] != ""
            assert dog["sex"] in ("male", "female")
            assert dog["castrated"] != ""

    async def test_seed_is_idempotent(self, db):
        """Running seed twice should not duplicate data (INSERT OR IGNORE)."""
        await generate_seed_data(db)
        await generate_seed_data(db)
        walkers = await fetch_all(db, "SELECT * FROM walkers")
        clients = await fetch_all(db, "SELECT * FROM clients")
        dogs = await fetch_all(db, "SELECT * FROM dogs")
        assert len(walkers) == 3
        assert len(clients) == 12
        assert len(dogs) == 14
