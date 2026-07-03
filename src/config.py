"""Walking Hounds — configuration via pydantic-settings."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Email IMAP ──────────────────────────────────────────
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""

    # ── Email SMTP ──────────────────────────────────────────
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # ── LLM ────────────────────────────────────────────────
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"

    # ── Database ────────────────────────────────────────────
    db_path: str = "data/walking_hounds.db"

    # ── Business ───────────────────────────────────────────
    walk_price_eur: float = 20.0
    late_cancel_hours: int = 24
    late_cancel_fee_percent: int = 50
    invoice_payment_address: str = "test-payment@walking-hounds.local"

    # ── Schedule ────────────────────────────────────────────
    walk_slots: str = "11:30,12:00,12:30"
    max_dogs_per_group: int = 4
    max_groups_per_day: int = 3
    min_groups_per_day: int = 2
    business_days: str = "mon,tue,wed,thu,fri"

    # ── Onboarding ─────────────────────────────────────────
    onboarding_rate_limit_per_min: int = 0  # 0 = disabled (test mode)

    # ── Intake ─────────────────────────────────────────────
    imap_poll_interval_sec: int = 60
    intake_confidence_threshold: float = 0.75
    imap_folder: str = "INBOX"  # Gmail label for plus-alias filtering
    intake_demo_mode: bool = False  # When True, match clients by name from body not email

    # ── Reminders ──────────────────────────────────────────
    reminder_hours_before_walk: int = 2
    reminder_poll_interval_sec: int = 60

    # ── Dashboard ──────────────────────────────────────────
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8010

    # ── Derived ────────────────────────────────────────────
    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def walk_slot_list(self) -> list[str]:
        return [s.strip() for s in self.walk_slots.split(",") if s.strip()]

    @property
    def business_day_list(self) -> list[str]:
        return [d.strip().lower() for d in self.business_days.split(",") if d.strip()]

    def ensure_dirs(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings
