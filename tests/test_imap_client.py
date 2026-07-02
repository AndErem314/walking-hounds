"""Tests for the IMAP client — body extraction logic.

The actual IMAP connection is not tested (requires a real server).
We test the static _extract_body method which parses raw email bytes.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from src.email.imap_client import IMAPClient


class TestExtractBody:
    """Test the _extract_body static method with various email formats."""

    def test_plain_text_email(self):
        msg = EmailMessage()
        msg.set_content("Hi, can you walk Bello on Friday?")
        body = IMAPClient._extract_body(msg)
        assert "walk Bello" in body

    def test_html_only_email(self):
        msg = MIMEMultipart("alternative")
        html_part = MIMEText(
            "<html><body><p>Can you walk <b>Bello</b> on Friday?</p></body></html>",
            "html",
        )
        msg.attach(html_part)
        body = IMAPClient._extract_body(msg)
        assert "Bello" in body
        assert "<html>" not in body  # tags stripped

    def test_multipart_with_plain_and_html(self):
        msg = MIMEMultipart("alternative")
        plain = MIMEText("Walk Bello Friday", "plain")
        html = MIMEText("<p>Walk <b>Bello</b> Friday</p>", "html")
        msg.attach(plain)
        msg.attach(html)
        body = IMAPClient._extract_body(msg)
        # Should prefer plain text
        assert "Bello" in body
        assert "<p>" not in body

    def test_empty_body(self):
        msg = EmailMessage()
        msg.set_content("")
        body = IMAPClient._extract_body(msg)
        assert body.strip() == ""

    def test_unicode_body(self):
        msg = EmailMessage()
        msg.set_content("Möchte einen Spaziergang am Freitag für Bello")
        body = IMAPClient._extract_body(msg)
        assert "Spaziergang" in body
        assert "Bello" in body
