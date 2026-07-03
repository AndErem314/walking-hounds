"""Async IMAP client for polling the booking inbox.

Uses aioimaplib for non-blocking IMAP operations.
Returns parsed email dictionaries: {message_id, from, subject, body, date}
"""

from __future__ import annotations

import email
import logging
from email import policy
from email.parser import BytesParser
from typing import Any

import aioimaplib

logger = logging.getLogger(__name__)


class IMAPClient:
    """Async IMAP poller for the walking-hounds inbox."""

    def __init__(
        self,
        host: str = "imap.gmail.com",
        port: int = 993,
        user: str = "",
        password: str = "",
    ):
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._client: aioimaplib.IMAP4 | None = None

    async def connect(self) -> None:
        """Connect and login to the IMAP server."""
        self._client = aioimaplib.IMAP4_SSL(host=self._host, port=self._port)
        await self._client.wait_hello_from_server()
        resp = await self._client.login(self._user, self._password)
        if resp.result != "OK":
            raise RuntimeError(
                f"IMAP login failed for {self._user}: {resp.result} — "
                f"check credentials (Gmail requires an App Password, not your account password)"
            )
        logger.info("IMAP connected: %s", self._user)

    async def disconnect(self) -> None:
        """Logout and close the connection."""
        if self._client:
            try:
                await self._client.logout()
            except Exception:
                pass
            self._client = None

    async def fetch_unseen(self, folder: str = "INBOX") -> list[dict[str, Any]]:
        """Fetch all unseen emails from *folder*.

        Returns list of dicts with keys:
            message_id, from_email, subject, body, date
        """
        if not self._client:
            raise RuntimeError("IMAPClient not connected — call connect() first")

        await self._client.select(folder)

        # Search for unseen messages
        status, responses = await self._client.search("UNSEEN")
        if status != "OK":
            logger.warning("IMAP search returned: %s", status)
            return []

        # Parse message IDs from response (only the first element contains IDs)
        ids = []
        if responses and isinstance(responses[0], bytes) and responses[0].strip():
            ids = [x for x in responses[0].decode().split() if x]

        logger.info("IMAP folder '%s': %d unseen", folder, len(ids))

        if not ids:
            return []

        emails: list[dict[str, Any]] = []
        for msg_id in ids:
            try:
                msg_data = await self._fetch_one(msg_id)
                if msg_data:
                    emails.append(msg_data)
            except Exception as exc:
                logger.warning("Failed to fetch message %s: %s", msg_id, exc)

        return emails

    async def fetch_all(self, folder: str = "INBOX", limit: int = 50) -> list[dict[str, Any]]:
        """Fetch all emails (seen and unseen) from *folder*, up to *limit*."""
        if not self._client:
            raise RuntimeError("IMAPClient not connected — call connect() first")

        await self._client.select(folder)

        status, responses = await self._client.search("ALL")
        if status != "OK":
            return []

        ids = []
        for resp in responses:
            if isinstance(resp, bytes) and resp.strip():
                ids.extend(resp.decode().split())

        if not ids:
            return []

        # Take the most recent *limit*
        ids = ids[-limit:]

        emails: list[dict[str, Any]] = []
        for msg_id in ids:
            try:
                msg_data = await self._fetch_one(msg_id)
                if msg_data:
                    emails.append(msg_data)
            except Exception as exc:
                logger.warning("Failed to fetch message %s: %s", msg_id, exc)

        return emails

    async def _fetch_one(self, msg_id: str) -> dict[str, Any] | None:
        """Fetch and parse a single email by its sequence number."""
        status, responses = await self._client.fetch(msg_id, "(RFC822)")
        if status != "OK":
            return None

        # Find the RFC822 body in the response.
        # aioimaplib returns flat data: [status_line, bytearray(body), ...]
        # Fallback: try tuple (older aioimaplib), then bytearray (current)
        raw_bytes = None
        for resp in responses:
            if isinstance(resp, tuple) and len(resp) >= 2:
                raw_bytes = resp[1]
                break
        if raw_bytes is None:
            for resp in responses:
                if isinstance(resp, (bytes, bytearray)) and len(resp) > 100:
                    raw_bytes = resp
                    break

        if not raw_bytes:
            return None

        msg = BytesParser(policy=policy.default).parsebytes(raw_bytes)

        # Extract fields
        message_id = str(msg.get("Message-ID", ""))
        from_email = str(msg.get("From", ""))
        subject = str(msg.get("Subject", ""))
        date_str = str(msg.get("Date", ""))

        # Extract body (prefer plain text)
        body = self._extract_body(msg)

        return {
            "message_id": message_id,
            "from_email": from_email,
            "subject": subject,
            "body": body,
            "date": date_str,
        }

    @staticmethod
    def _extract_body(msg: email.Message) -> str:
        """Extract the plain text body from an email message."""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                if content_type == "text/plain" and "attachment" not in content_disposition:
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        try:
                            return payload.decode(charset, errors="replace")
                        except (LookupError, UnicodeDecodeError):
                            return payload.decode("utf-8", errors="replace")
            # No plain text found — try HTML and strip tags
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        html = payload.decode(charset, errors="replace")
                        # Very basic HTML tag stripping
                        import re
                        text = re.sub(r"<[^>]+>", "", html)
                        text = re.sub(r"\s+", " ", text).strip()
                        return text
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    return payload.decode("utf-8", errors="replace")

        return ""
