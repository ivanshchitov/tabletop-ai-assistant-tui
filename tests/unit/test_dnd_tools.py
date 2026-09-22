"""Инструменты собственного MCP-сервера: схемы входа, валидация аргументов, текст результата.

Протокол здесь не поднимается — проверяется чистый слой инструментов против локальной
заглушки внешнего API.
"""

import json

import pytest

from mcp_server.dnd_tools import DndTools, TOOLS
from tests.dnd_api_stub import DndAPIStub


@pytest.fixture
def stub():
    server = DndAPIStub()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def tools(stub):
    from mcp_server.dnd_api import DndAPI

    return DndTools(DndAPI(url=stub.url))


# --- регистрация и схемы ---


def test_three_tools_are_declared():
    assert len(TOOLS) == 3


def test_every_tool_has_name_and_description():
    for tool in TOOLS:
        assert tool.name
        assert len(tool.description) > 10


def test_schemas_declare_types_and_required_fields():
    schemas = {tool.name: tool.input_schema for tool in TOOLS}
    for schema in schemas.values():
        assert schema["type"] == "object"
        for parameter in schema["properties"].values():
            assert parameter["type"] in {"string", "integer"}
            assert parameter["description"]
    entry = next(tool for tool in TOOLS if "required" in tool.input_schema and tool.input_schema["required"])
    assert entry.input_schema["required"]


def test_ruleset_parameter_is_optional_with_allowed_values():
    for tool in TOOLS:
        ruleset = tool.input_schema["properties"].get("ruleset")
        if ruleset is None:
            continue
        assert ruleset["enum"] == ["2014", "2024"]
        assert ruleset["default"] == "2014"
        assert "ruleset" not in tool.input_schema.get("required", [])


# --- вызовы ---


def test_sections_tool_lists_sections_from_api(tools):
    text = tools.call("dnd_sections", {})
    assert "monsters" in text
    assert "spells" in text


def test_entry_tool_returns_record(tools):
    text = tools.call("dnd_entry", {"section": "monsters", "index": "goblin"})
    assert "Goblin" in text
    assert "hit_points" in text
    assert "7" in text


def test_search_tool_returns_matches(tools):
    text = tools.call("dnd_search", {"section": "monsters", "query": "gob"})
    assert "goblin" in text
    assert "hobgoblin" in text


def test_search_tool_respects_limit(tools):
    text = tools.call("dnd_search", {"section": "monsters", "query": "gob", "limit": 1})
    assert "goblin" in text
    assert "hobgoblin" not in text


def test_default_ruleset_is_used(tools, stub):
    tools.call("dnd_entry", {"section": "monsters", "index": "goblin"})
    assert stub.paths[-1] == "/api/2014/monsters/goblin"


def test_explicit_ruleset_is_used(tools, stub):
    text = tools.call("dnd_entry", {"section": "monsters", "index": "goblin", "ruleset": "2024"})
    assert stub.paths[-1] == "/api/2024/monsters/goblin"
    assert "Goblin (2024)" in text


# --- отказы ---


def test_missing_required_argument_is_reported_without_http(tools, stub):
    text = tools.call("dnd_entry", {"section": "monsters"})
    assert "index" in text
    assert stub.paths == []


def test_empty_required_argument_is_reported_without_http(tools, stub):
    text = tools.call("dnd_entry", {"section": "monsters", "index": "   "})
    assert "index" in text
    assert stub.paths == []


def test_unknown_ruleset_is_reported_without_http(tools, stub):
    text = tools.call("dnd_entry", {"section": "monsters", "index": "goblin", "ruleset": "1999"})
    assert "2014" in text and "2024" in text
    assert stub.paths == []


def test_limit_out_of_range_is_reported_without_http(tools, stub):
    text = tools.call("dnd_search", {"section": "monsters", "query": "gob", "limit": 99})
    assert "limit" in text
    assert stub.paths == []


def test_unknown_section_lists_available_sections(tools, stub):
    text = tools.call("dnd_entry", {"section": "нет-такого", "index": "goblin"})
    assert "monsters" in text
    assert all("нет-такого" not in path for path in stub.paths)


def test_unknown_tool_is_reported(tools):
    text = tools.call("dnd_unknown", {})
    assert "dnd_unknown" in text


def test_api_failure_becomes_text(tools, stub):
    stub.failure = 500
    text = tools.call("dnd_entry", {"section": "monsters", "index": "goblin"})
    assert "500" in text or "недоступен" in text


def test_entry_result_is_readable_json(tools):
    text = tools.call("dnd_entry", {"section": "monsters", "index": "goblin"})
    payload = json.loads(text[text.index("{") :])
    assert payload["name"] == "Goblin"
