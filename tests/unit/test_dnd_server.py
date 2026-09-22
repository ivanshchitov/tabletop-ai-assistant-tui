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
def client(stub, monkeypatch):
    monkeypatch.setenv("TABLETOP_DND_API_URL", stub.url)
    spec = config.MCPServerSpec(
        name="dnd",
        transport="stdio",
        command=sys.executable,
        args=(str(DND_SERVER),),
        env_keys=("TABLETOP_DND_API_URL",),
        description="свой сервер прогона",
    )
    return MCPClient(spec)


def test_server_declares_three_tools(client):
    connection = client.connect()
    assert len(connection.tools) == 3


def test_tools_have_names_and_descriptions(client):
    connection = client.connect()
    for tool in connection.tools:
        assert tool.name.startswith("dnd_")
        assert len(tool.description) > 10


def test_handshake_reports_server_name(client):
    connection = client.connect()
    assert connection.server_name
    assert connection.protocol_version
