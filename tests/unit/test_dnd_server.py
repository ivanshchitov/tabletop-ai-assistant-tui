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
def client(stub, schedule_file, monkeypatch):
    monkeypatch.setenv("TABLETOP_DND_API_URL", stub.url)
    spec = config.MCPServerSpec(
        name="dnd",
        transport="stdio",
        command=sys.executable,
        args=(str(DND_SERVER),),
        env_keys=("TABLETOP_DND_API_URL", "TABLETOP_SCHEDULE_FILE"),
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
