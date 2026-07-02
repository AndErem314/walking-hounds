"""Async Ollama client for email parsing and intent classification.

Uses the Ollama HTTP API (aiohttp) — no SDK dependency needed.
The LLM is prompted to return structured JSON for reliable parsing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class OllamaClient:
    """Lightweight async client for Ollama's generate API."""

    def __init__(self, host: str = "http://localhost:11434", model: str = "llama3.1:8b"):
        self._host = host.rstrip("/")
        self._model = model
        self._session: aiohttp.ClientSession | None = None

    async def init(self) -> None:
        self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("OllamaClient not initialised — call init() first")
        return self._session

    async def generate(self, prompt: str, *, system: str = "", temperature: float = 0.3) -> str:
        """Send a generate request to Ollama, return the raw text response."""
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if system:
            payload["system"] = system

        async with self.session.post(
            f"{self._host}/api/generate",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=120),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data.get("response", "")

    async def generate_json(self, prompt: str, *, system: str = "", temperature: float = 0.3) -> dict:
        """Send a generate request and parse the response as JSON.

        Handles common LLM quirks:
        - Text before/after the JSON block
        - Markdown code fences (```json ... ```)
        - Trailing commas
        """
        raw = await self.generate(prompt, system=system, temperature=temperature)
        return self._extract_json(raw)

    @staticmethod
    def _extract_json(text: str) -> dict:
        """Extract a JSON object from LLM output that may contain extra text."""
        # Try direct parse first
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try removing markdown code fences
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.rindex("```")
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.rindex("```")
            text = text[start:end].strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Try finding the first { and last }
        first = text.find("{")
        last = text.rfind("}")
        if first != -1 and last != -1:
            try:
                return json.loads(text[first:last + 1])
            except json.JSONDecodeError:
                pass

        logger.warning("Could not parse JSON from LLM response: %s...", text[:200])
        return {}

    async def is_available(self) -> bool:
        """Check if Ollama is running."""
        try:
            async with self.session.get(
                f"{self._host}/api/tags",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status == 200
        except Exception:
            return False
