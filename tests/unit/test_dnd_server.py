"""Собственный MCP-сервер проекта как отдельный процесс: состав инструментов, признак ошибки, цепочка.

Сервер поднимается интерпретатором прогона, а внешний API подменён локальной заглушкой через
переменную окружения — поэтому проверка не зависит ни от сети, ни от публичного сервиса.
Логика инструментов покрыта без процесса (`test_dnd_tools.py`, `test_scheduler.py`,
`test_pipeline_tools.py`); здесь — только то, что проверяется сквозь протокол: общий список
инструментов, диспетчеризация по имени с признаком ошибки и сквозная цепочка вызовов.
"""

import sys
from pathlib import Path

import pytest

from core import config
from core.mcp_client import MCPClient, MCPError
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
def exports(tmp_path, monkeypatch):
    """Сохранение пишет файлы: без подмены прогон писал бы в реальный exports/."""
    path = tmp_path / "exports"
    monkeypatch.setenv("TABLETOP_EXPORTS_DIR", str(path))
    return path


@pytest.fixture
def client(stub, exports, monkeypatch):
    monkeypatch.setenv("TABLETOP_DND_API_URL", stub.url)
    spec = config.MCPServerSpec(
        name="dnd",
        transport="stdio",
        command=sys.executable,
        args=(str(DND_SERVER),),
        env_keys=("TABLETOP_DND_API_URL", "TABLETOP_EXPORTS_DIR"),
        description="свой сервер прогона",
    )
    return MCPClient(spec)


def test_server_declares_all_tools(client):
    """Справочные, планировочные и конвейерные инструменты — одним списком от одного сервера."""
    names = {tool.name for tool in client.connect().tools}
    assert {"dnd_sections", "dnd_search", "dnd_entry", "dnd_digest"} <= names
    assert {"schedule_add", "schedule_list", "schedule_run_due", "schedule_summary"} <= names
    assert {"dnd_summarize", "save_to_file"} <= names


def test_tool_failure_is_marked_as_error(client):
    """Отказ — протокольный признак ошибки, а не данные: цепочка не передаст его дальше."""
    with pytest.raises(MCPError, match="Неверные аргументы"):
        client.call_tool("dnd_search", {"section": "spells"})


def test_search_summarize_save_through_the_protocol(client, exports):
    """Три вызова по протоколу: выход каждого — дословно вход следующего."""
    found = client.call_tool("dnd_search", {"section": "spells", "query": "fire", "details": True})
    summary = client.call_tool("dnd_summarize", {"text": found})
    assert "Fireball" in summary and "Fire Bolt" in summary
    saved = client.call_tool("save_to_file", {"text": summary, "filename": "fire-spells"})
    target = exports / "fire-spells.md"
    assert target.read_text(encoding="utf-8") == summary
    assert f"{len(summary)} символ" in saved
