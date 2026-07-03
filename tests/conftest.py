"""Shared pytest fixtures for Walking Hounds tests."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

# Set test env BEFORE importing app modules
_tmpdir = tempfile.mkdtemp(prefix="wh_test_")
os.environ["DB_PATH"] = os.path.join(_tmpdir, "test.db")
os.environ["IMAP_USER"] = "test@example.com"
os.environ["IMAP_PASSWORD"] = "testpass"
os.environ["SMTP_USER"] = "test@example.com"
os.environ["SMTP_PASSWORD"] = "testpass"


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    """Return a fresh DB path in a tmp directory."""
    return str(tmp_path / "test.db")


@pytest.fixture
def settings(tmp_db_path: str):
    """Override settings to use tmp DB."""
    from src.config import Settings
    s = Settings(db_path=tmp_db_path, intake_demo_mode=False)
    s.ensure_dirs()
    return s
