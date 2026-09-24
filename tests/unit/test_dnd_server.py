"""Собственный MCP-сервер проекта как отдельный процесс: рукопожатие, инструменты, вызовы.

Сервер поднимается интерпретатором прогона, а внешний API подменён локальной заглушкой через
переменную окружения — поэтому проверка не зависит ни от сети, ни от публичного сервиса.
"""

import sys
from pathlib import Path

import pytest

from core import config
from core.mcp_client import MCPClient
from tests.dnd_api_stub import DndAPIStub

DND_SERVER = Path(__file__).resolve().parent.parent.parent / "mcp_server" / "dnd_server.py"


@pytest.fixture
def stub():
    server = DndAPIStub()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def schedule_file(tmp_path, monkeypatch):
    """Планировщик сервера пишет файл: без подмены прогон писал бы в реальный schedule.json."""
    path = tmp_path / "schedule.json"
    monkeypatch.setenv("TABLETOP_SCHEDULE_FILE", str(path))
    return path


@pytest.fixture
def exports(tmp_path, monkeypatch):
    """Сохранение пишет файлы: без подмены прогон писал бы в реальный exports/."""
    path = tmp_path / "exports"
    monkeypatch.setenv("TABLETOP_EXPORTS_DIR", str(path))
    return path


@pytest.fixture
def client(stub, schedule_file, exports, monkeypatch):
    monkeypatch.setenv("TABLETOP_DND_API_URL", stub.url)
    spec = config.MCPServerSpec(
        name="dnd",
        transport="stdio",
        command=sys.executable,
        args=(str(DND_SERVER),),
        env_keys=("TABLETOP_DND_API_URL", "TABLETOP_SCHEDULE_FILE", "TABLETOP_EXPORTS_DIR"),
        description="свой сервер прогона",
    )
    return MCPClient(spec)


def test_server_declares_reference_and_scheduler_tools(client):
    """Справочные инструменты и планировщик приходят одним списком от одного сервера."""
    connection = client.connect()
    names = {tool.name for tool in connection.tools}
    assert {"dnd_sections", "dnd_search", "dnd_entry", "dnd_digest"} <= names
    assert {"schedule_add", "schedule_list", "schedule_run_due", "schedule_summary"} <= names


def test_tools_have_names_and_descriptions(client):
    connection = client.connect()
    for tool in connection.tools:
        assert tool.name
        assert len(tool.description) > 10


def test_scheduler_tools_declare_their_schemas(client):
    connection = client.connect()
    add = next(tool for tool in connection.tools if tool.name == "schedule_add")
    assert "tool" in add.input_schema["properties"]
    assert "every_minutes" in add.input_schema["properties"]


def test_added_job_is_written_to_the_schedule_file(client, schedule_file):
    """Постановка задания идёт по протоколу, а состояние оказывается в подменённом файле."""
    import json

    text = client.call_tool(
        "schedule_add",
        {"tool": "dnd_digest", "arguments": {"section": "monsters"}, "every_minutes": 5},
    )
    assert "dnd_digest" in text
    data = json.loads(schedule_file.read_text(encoding="utf-8"))
    assert data["jobs"][0]["tool"] == "dnd_digest"


def test_due_job_is_executed_through_the_protocol(client, schedule_file):
    import json

    client.call_tool(
        "schedule_add",
        {"tool": "dnd_digest", "arguments": {"section": "monsters"}, "every_minutes": 5},
    )
    text = client.call_tool("schedule_run_due", {})
    assert "выполнено" in text.lower()

    data = json.loads(schedule_file.read_text(encoding="utf-8"))
    assert data["runs"][0]["ok"] is True
    assert data["collected"]["monsters/2014"]


def test_handshake_reports_server_name(client):
    connection = client.connect()
    assert connection.server_name
    assert connection.protocol_version


# --- признак ошибки (день 19) ---


def test_reference_tool_failure_is_marked_as_error(client):
    """Отказ — протокольный признак ошибки, а не данные: цепочка не передаст его дальше."""
    from core.mcp_client import MCPError

    with pytest.raises(MCPError, match="Неверные аргументы"):
        client.call_tool("dnd_search", {"section": "spells"})


def test_scheduler_tool_failure_is_marked_as_error(client):
    from core.mcp_client import MCPError

    with pytest.raises(MCPError, match="Неверные аргументы"):
        client.call_tool("schedule_add", {"tool": "", "every_minutes": 5})


def test_successful_call_is_not_an_error(client):
    assert "fireball" in client.call_tool("dnd_search", {"section": "spells", "query": "fire"})


# --- цепочка через протокол (день 19) ---


def test_server_declares_pipeline_tools(client):
    names = {tool.name for tool in client.connect().tools}
    assert {"dnd_summarize", "save_to_file"} <= names


def test_search_summarize_save_through_the_protocol(client, exports):
    """Три вызова по протоколу: выход каждого — дословно вход следующего."""
    found = client.call_tool("dnd_search", {"section": "spells", "query": "fire", "details": True})
    summary = client.call_tool("dnd_summarize", {"text": found})
    assert "Fireball" in summary and "Fire Bolt" in summary
    saved = client.call_tool("save_to_file", {"text": summary, "filename": "fire-spells"})
    target = exports / "fire-spells.md"
    assert target.read_text(encoding="utf-8") == summary
    assert f"{len(summary)} символ" in saved
