"""Контракт реестра: каждый настоящий MCP-сервер поднимается и отдаёт инструменты.

Маркер `network`: по умолчанию не запускается (`addopts` снимает его), потому что сервер
скачивается и запускается из сети. Именно поэтому пропажа внешнего сервера красит отдельный
прогон, а не основную батарею — остальные тесты подключения работают против фейкового сервера.
"""

import pytest

from core import config
from core.mcp_client import MCPClient

pytestmark = pytest.mark.network


@pytest.mark.parametrize("name", sorted(config.MCP_SERVERS))
def test_registry_server_connects_and_lists_tools(name):
    connection = MCPClient(config.MCP_SERVERS[name]).connect()

    assert connection.server_name
    assert connection.protocol_version
    assert connection.tools, "сервер реестра обязан объявить хотя бы один инструмент"
    for tool in connection.tools:
        assert tool.name
