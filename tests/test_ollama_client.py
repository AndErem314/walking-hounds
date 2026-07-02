"""Tests for the OllamaClient — uses a mock aiohttp session.

No real Ollama instance needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.llm.ollama_client import OllamaClient


def _make_mock_response(json_data=None, status=200):
    """Create a mock that works as an async context manager for aiohttp."""
    resp = MagicMock()
    resp.status = status
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data or {})
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=None)
    return resp


def _make_mock_session(resp=None, get_resp=None, get_side_effect=None):
    """Create a mock aiohttp.ClientSession.

    Use MagicMock (not AsyncMock) because session.post() should return
    an object directly (the context manager), not a coroutine.
    """
    session = MagicMock()
    if resp is not None:
        session.post.return_value = resp
    if get_resp is not None:
        session.get.return_value = get_resp
    if get_side_effect is not None:
        session.get.side_effect = get_side_effect
    session.close = AsyncMock()
    return session


class TestOllamaClient:
    async def test_init_and_close(self):
        client = OllamaClient(host="http://localhost:11434", model="llama3.1:8b")
        await client.init()
        assert client.session is not None
        await client.close()
        assert client._session is None

    async def test_generate_returns_response_text(self):
        client = OllamaClient()
        await client.init()

        mock_resp = _make_mock_response({"response": "Hello from LLM"})
        client._session = _make_mock_session(resp=mock_resp)

        result = await client.generate("Say hello")
        assert result == "Hello from LLM"

    async def test_generate_json_parses_response(self):
        client = OllamaClient()
        await client.init()

        json_response = '{"intent": "booking", "confidence": 0.95}'
        mock_resp = _make_mock_response({"response": json_response})
        client._session = _make_mock_session(resp=mock_resp)

        result = await client.generate_json("Parse this email")
        assert result["intent"] == "booking"
        assert result["confidence"] == 0.95

    async def test_generate_json_handles_markdown_fences(self):
        client = OllamaClient()
        await client.init()

        response_with_fences = 'Here is the result:\n```json\n{"intent": "query", "confidence": 0.8}\n```\nDone.'
        mock_resp = _make_mock_response({"response": response_with_fences})
        client._session = _make_mock_session(resp=mock_resp)

        result = await client.generate_json("Parse")
        assert result["intent"] == "query"
        assert result["confidence"] == 0.8

    async def test_generate_json_handles_extra_text(self):
        """LLM sometimes wraps JSON in explanatory text."""
        client = OllamaClient()
        await client.init()

        response = 'I think this is a booking request.\n{"intent": "booking", "confidence": 0.9, "dog_name": "Bello"}\nHope this helps!'
        mock_resp = _make_mock_response({"response": response})
        client._session = _make_mock_session(resp=mock_resp)

        result = await client.generate_json("Parse")
        assert result["intent"] == "booking"
        assert result["dog_name"] == "Bello"

    async def test_generate_json_returns_empty_on_garbage(self):
        client = OllamaClient()
        await client.init()

        mock_resp = _make_mock_response({"response": "I cannot parse this"})
        client._session = _make_mock_session(resp=mock_resp)

        result = await client.generate_json("Parse")
        assert result == {}

    async def test_is_available_true(self):
        client = OllamaClient()
        await client.init()

        mock_resp = _make_mock_response(status=200)
        client._session = _make_mock_session(get_resp=mock_resp)

        assert await client.is_available() is True

    async def test_is_available_false_on_error(self):
        client = OllamaClient()
        await client.init()

        client._session = _make_mock_session(get_side_effect=Exception("Connection refused"))

        assert await client.is_available() is False


class TestOllamaJsonExtraction:
    """Test the _extract_json static method directly — no network needed."""

    def test_direct_json(self):
        result = OllamaClient._extract_json('{"key": "value"}')
        assert result["key"] == "value"

    def test_markdown_fenced_json(self):
        result = OllamaClient._extract_json('```json\n{"key": "value"}\n```')
        assert result["key"] == "value"

    def test_plain_fenced_json(self):
        result = OllamaClient._extract_json('```\n{"key": "value"}\n```')
        assert result["key"] == "value"

    def test_json_with_surrounding_text(self):
        result = OllamaClient._extract_json('Here is the result:\n{"key": "value"}\nDone.')
        assert result["key"] == "value"

    def test_nested_json(self):
        result = OllamaClient._extract_json('{"outer": {"inner": "value"}}')
        assert result["outer"]["inner"] == "value"

    def test_garbage_returns_empty(self):
        result = OllamaClient._extract_json("No JSON here at all")
        assert result == {}

    def test_empty_string(self):
        result = OllamaClient._extract_json("")
        assert result == {}

    def test_partial_json_returns_empty(self):
        result = OllamaClient._extract_json('{"key": "value"')  # missing closing brace
        assert result == {}
