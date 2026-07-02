"""Async SMTP client for sending outbound emails.

Uses aiosmtplib for non-blocking SMTP operations.
"""

from __future__ import annotations

import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import aiosmtplib

logger = logging.getLogger(__name__)


class SMTPClient:
    """Async SMTP sender for the walking-hounds system."""

    def __init__(
        self,
        host: str = "smtp.gmail.com",
        port: int = 587,
        user: str = "",
        password: str = "",
    ):
        self._host = host
        self._port = port
        self._user = user
        self._password = password

    async def send(
        self,
        to_email: str,
        subject: str,
        body: str,
        *,
        html: bool = False,
    ) -> bool:
        """Send an email. Returns True on success, False on failure."""
        if not to_email:
            logger.warning("SMTPClient: no recipient address")
            return False

        msg = MIMEMultipart("alternative")
        msg["From"] = self._user
        msg["To"] = to_email
        msg["Subject"] = subject

        if html:
            msg.attach(MIMEText(body, "html"))
            # Also attach plain text version for non-HTML clients
            import re
            plain = re.sub(r"<[^>]+>", "", body)
            plain = re.sub(r"\s+", " ", plain).strip()
            msg.attach(MIMEText(plain, "plain"))
        else:
            msg.attach(MIMEText(body, "plain"))

        try:
            await aiosmtplib.send(
                msg,
                hostname=self._host,
                port=self._port,
                username=self._user,
                password=self._password,
                start_tls=True,
            )
            logger.info("SMTPClient: sent '%s' to %s", subject, to_email)
            return True
        except Exception as exc:
            logger.error("SMTPClient: failed to send to %s: %s", to_email, exc)
            return False
