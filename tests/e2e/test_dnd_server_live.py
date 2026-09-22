"""Контракт собственного сервера против настоящего API правил D&D 5e.

Маркер `network`: по умолчанию не запускается (`addopts` снимает его). Остальные проверки
сервера работают против локальной заглушки, поэтому недоступность публичного сервиса красит
только этот прогон, а не основную батарею.
"""

import json
import os

import pytest

from core import config
from core.mcp_client import MCPClient

pytestmark = pytest.mark.network

SPEC = config.MCP_SERVERS["dnd-rules"]


@pytest.fixture(autouse=True)
def without_stub_url(monkeypatch):
    """Живой прогон обязан идти в публичный API, а не в заглушку соседнего теста."""
    monkeypatch.delenv("TABLETOP_DND_API_URL", raising=False)
    assert "TABLETOP_DND_API_URL" not in os.environ


def test_server_connects_and_declares_its_tools():
    connection = MCPClient(SPEC).connect()

    assert connection.server_name
    assert connection.protocol_version
    assert len(connection.tools) == 3
    for tool in connection.tools:
        assert tool.description
        assert tool.input_schema.get("type") == "object"


def test_sections_tool_returns_real_sections():
    text = MCPClient(SPEC).call_tool("dnd_sections", {})

    assert "monsters" in text
    assert "spells" in text


def test_entry_tool_returns_a_real_record():
    text = MCPClient(SPEC).call_tool("dnd_entry", {"section": "monsters", "index": "goblin"})
    payload = json.loads(text[text.index("{") :])

    assert payload["name"].lower().startswith("goblin")
    assert payload["hit_points"] > 0


def test_search_tool_finds_real_entries():
    text = MCPClient(SPEC).call_tool(
        "dnd_search", {"section": "spells", "query": "fire", "limit": 3}
    )

    assert "fireball" in text


def test_second_ruleset_is_reachable():
    text = MCPClient(SPEC).call_tool("dnd_sections", {"ruleset": "2024"})

    assert "monsters" in text
