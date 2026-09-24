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


def test_reference_and_digest_tools_are_declared():
    """Справочных инструментов три; к ним добавился сбор данных с накоплением (день 18)."""
    assert {tool.name for tool in TOOLS} == {"dnd_sections", "dnd_search", "dnd_entry", "dnd_digest"}


def test_every_tool_has_name_and_description():
    for tool in TOOLS:
        assert tool.name
        assert len(tool.description) > 10


def test_schemas_declare_types_and_required_fields():
    schemas = {tool.name: tool.input_schema for tool in TOOLS}
    for schema in schemas.values():
        assert schema["type"] == "object"
        for parameter in schema["properties"].values():
            # boolean — флаг подробностей поиска (день 19).
            assert parameter["type"] in {"string", "integer", "boolean"}
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


# --- сбор данных с накоплением ---


@pytest.fixture
def digest_tools(stub, tmp_path):
    """Инструменты со своим хранилищем планировщика: накопление проверяется между вызовами."""
    from core.schedule_store import ScheduleStore
    from mcp_server.dnd_api import DndAPI

    return DndTools(DndAPI(url=stub.url), store=ScheduleStore(path=tmp_path / "schedule.json"))


def test_digest_is_declared_with_its_schema():
    digest = next(tool for tool in TOOLS if tool.name == "dnd_digest")
    assert "section" in digest.input_schema["properties"]
    assert digest.input_schema["required"] == ["section"]


def test_first_digest_reports_everything_as_fresh(digest_tools):
    text = digest_tools.call("dnd_digest", {"section": "monsters"})
    assert "monsters" in text
    assert "Goblin" in text
    assert "впервые: 2" in text


def test_repeated_digest_reports_no_fresh_entries(digest_tools):
    digest_tools.call("dnd_digest", {"section": "monsters"})
    text = digest_tools.call("dnd_digest", {"section": "monsters"})
    assert "впервые: 0" in text
    assert digest_tools.store.collected("monsters/2014") == ("Goblin", "Hobgoblin")


def test_new_entry_in_the_external_api_is_reported_as_fresh(digest_tools, monkeypatch):
    from tests import dnd_api_stub

    digest_tools.call("dnd_digest", {"section": "monsters"})
    monkeypatch.setitem(
        dnd_api_stub.ENTRIES["monsters"], "orc", {"index": "orc", "name": "Orc", "hit_points": 15}
    )
    text = digest_tools.call("dnd_digest", {"section": "monsters"})
    assert "впервые: 1" in text
    assert "Orc" in text


def test_digest_keeps_rulesets_apart(digest_tools):
    digest_tools.call("dnd_digest", {"section": "monsters"})
    text = digest_tools.call("dnd_digest", {"section": "monsters", "ruleset": "2024"})
    assert "впервые: 2" in text
    assert digest_tools.store.collected("monsters/2024") == ("Goblin (2024)", "Hobgoblin (2024)")


def test_digest_rejects_unknown_section_without_calling_the_api(digest_tools, stub):
    stub.paths.clear()
    text = digest_tools.call("dnd_digest", {"section": "таверны"})
    assert "неизвестен" in text
    assert all("/таверны" not in path for path in stub.paths)


# --- поиск с подробностями (день 19) ---


def test_search_details_is_an_optional_boolean():
    search = next(tool for tool in TOOLS if tool.name == "dnd_search")
    details = search.input_schema["properties"]["details"]
    assert details["type"] == "boolean"
    assert details["default"] is False
    assert "details" not in search.input_schema["required"]


def test_search_without_details_keeps_the_plain_list(tools, stub):
    text = tools.call("dnd_search", {"section": "spells", "query": "fire"})
    assert "- fireball: Fireball" in text
    # Без подробностей записи не запрашиваются: прежний поиск — один запрос списка.
    assert not any(path.endswith("/spells/fireball") for path in stub.paths)


def test_search_details_carries_entry_fields_as_json(tools):
    """Подробный поиск — вход для сводки: основные поля записей в машиночитаемом виде."""
    text = tools.call("dnd_search", {"section": "spells", "query": "fire", "details": True})
    header, _, body = text.partition("\n")
    assert "spells" in header
    records = json.loads(body)
    assert [record["index"] for record in records] == ["fireball", "fire-bolt"]
    fireball = records[0]
    assert fireball["name"] == "Fireball"
    assert fireball["level"] == 3
    assert fireball["school"] == "Evocation"
    assert fireball["desc"].startswith("Взрыв пламени")


def test_search_details_accepts_string_flag(tools):
    """Ручной `/tool call` передаёт значения строками: «true» тоже включает подробности."""
    text = tools.call("dnd_search", {"section": "spells", "query": "fire", "details": "true"})
    assert json.loads(text.partition("\n")[2])[0]["index"] == "fireball"
