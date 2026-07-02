"""Tests for configuration and settings."""

from __future__ import annotations

import os
import tempfile

from src.config import Settings


class TestSettings:
    def test_default_values(self):
        s = Settings()
        assert s.imap_host == "imap.gmail.com"
        assert s.imap_port == 993
        assert s.smtp_host == "smtp.gmail.com"
        assert s.smtp_port == 587
        assert s.ollama_host == "http://localhost:11434"
        assert s.ollama_model == "llama3.1:8b"
        assert s.walk_price_eur == 20.0
        assert s.max_dogs_per_group == 4
        assert s.max_groups_per_day == 3
        assert s.late_cancel_hours == 24
        assert s.late_cancel_fee_percent == 50

    def test_walk_slot_list(self):
        s = Settings(walk_slots="11:30,12:00,12:30")
        assert s.walk_slot_list == ["11:30", "12:00", "12:30"]

    def test_walk_slot_list_with_spaces(self):
        s = Settings(walk_slots="11:30, 12:00, 12:30")
        assert s.walk_slot_list == ["11:30", "12:00", "12:30"]

    def test_business_day_list(self):
        s = Settings(business_days="mon,tue,wed,thu,fri")
        assert s.business_day_list == ["mon", "tue", "wed", "thu", "fri"]

    def test_db_url_property(self):
        s = Settings(db_path="/tmp/test.db")
        assert s.db_url == "sqlite+aiosqlite:////tmp/test.db"

    def test_ensure_dirs_creates_parent(self, tmp_path):
        db_path = str(tmp_path / "deep" / "nested" / "test.db")
        s = Settings(db_path=db_path)
        s.ensure_dirs()
        assert os.path.isdir(tmp_path / "deep" / "nested")

    def test_env_file_loading(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "IMAP_USER=loaded@example.com\n"
            "WALK_PRICE_EUR=25.5\n"
            "OLLAMA_MODEL=mistral\n"
        )
        monkeypatch.chdir(tmp_path)
        # Clear env vars that conftest sets, so .env file takes priority
        monkeypatch.delenv("IMAP_USER", raising=False)
        monkeypatch.delenv("WALK_PRICE_EUR", raising=False)
        monkeypatch.delenv("OLLAMA_MODEL", raising=False)
        s = Settings(_env_file=str(env_file))
        assert s.imap_user == "loaded@example.com"
        assert s.walk_price_eur == 25.5
        assert s.ollama_model == "mistral"
