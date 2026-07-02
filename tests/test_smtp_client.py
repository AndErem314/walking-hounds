"""Tests for the SMTP client — uses mock aiosmtplib.send."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from src.email.smtp_client import SMTPClient


class TestSMTPClient:
    async def test_send_success(self):
        client = SMTPClient(
            host="smtp.gmail.com",
            port=587,
            user="test@example.com",
            password="testpass",
        )

        with patch("src.email.smtp_client.aiosmtplib.send", new_callable=AsyncMock) as mock_send:
            result = await client.send(
                to_email="client@example.com",
                subject="Test Subject",
                body="Test Body",
            )
            assert result is True
            mock_send.assert_called_once()

    async def test_send_failure_returns_false(self):
        client = SMTPClient(
            host="smtp.gmail.com",
            port=587,
            user="test@example.com",
            password="testpass",
        )

        with patch("src.email.smtp_client.aiosmtplib.send", new_callable=AsyncMock) as mock_send:
            mock_send.side_effect = Exception("SMTP connection refused")
            result = await client.send(
                to_email="client@example.com",
                subject="Test",
                body="Body",
            )
            assert result is False

    async def test_send_empty_recipient_returns_false(self):
        client = SMTPClient()
        result = await client.send(
            to_email="",
            subject="Test",
            body="Body",
        )
        assert result is False

    async def test_send_html_includes_plain_text(self):
        client = SMTPClient(
            host="smtp.gmail.com",
            port=587,
            user="test@example.com",
            password="testpass",
        )

        with patch("src.email.smtp_client.aiosmtplib.send", new_callable=AsyncMock) as mock_send:
            result = await client.send(
                to_email="client@example.com",
                subject="HTML Test",
                body="<html><body><p>Hello</p></body></html>",
                html=True,
            )
            assert result is True
            # Check the message had both parts
            sent_msg = mock_send.call_args[0][0]
            assert sent_msg.is_multipart()
