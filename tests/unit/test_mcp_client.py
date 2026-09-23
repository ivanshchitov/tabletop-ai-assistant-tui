"""MCP-клиент против фейкового stdio-сервера: рукопожатие, список инструментов, отказы.

Внешний сервер из реестра здесь не поднимается: фейковый сервер запускается интерпретатором
прогона, поэтому тесты не ходят в сеть и не зависят от npx.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from core import config
from core.mcp_client import MCPError, MCPClient

FAKE_SERVER = Path(__file__).resolve().parent.parent / "fake_mcp_server.py"


def fake_spec(*flags: str) -> config.MCPServerSpec:
    return config.MCPServerSpec(
        name="фейковый",
        transport="stdio",
        command=sys.executable,
        args=(str(FAKE_SERVER),) + flags,
        env_keys=(),
        description="фейковый сервер прогона",
    )


def test_connect_completes_handshake():
    client = MCPClient(fake_spec())
    connection = client.connect()
    assert connection.server_name == "фейковый-сервер"
    assert connection.server_version == "9.9.9"
    assert connection.protocol_version == "2025-06-18"


def test_tools_are_returned_with_names_and_descriptions():
    connection = MCPClient(fake_spec()).connect()
    names = [tool.name for tool in connection.tools]
    assert names == ["fake_search", "fake_details", "fake_echo"]
    assert connection.tools[0].description == "Поиск по фейковому каталогу"


def test_server_without_tools_is_not_an_error():
    """Пустой список — допустимый ответ, а не отказ подключения."""
    connection = MCPClient(fake_spec("--empty")).connect()
    assert connection.tools == []
    assert connection.server_name == "фейковый-сервер"


def test_missing_command_raises_mcp_error():
    spec = fake_spec()._replace(command="нет-такой-команды-на-диске")
    with pytest.raises(MCPError) as excinfo:
        MCPClient(spec).connect()
    assert str(excinfo.value)


def test_server_not_speaking_protocol_raises_mcp_error():
    with pytest.raises(MCPError) as excinfo:
        MCPClient(fake_spec("--garbage")).connect()
    assert str(excinfo.value)


def test_no_server_process_is_left_running():
    """Соединение поднимается по требованию и закрывается: висящих процессов не остаётся."""
    marker = "fake_mcp_server.py"
    MCPClient(fake_spec()).connect()
    running = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    assert running.stdout.strip() == ""


def test_secret_environment_variables_are_passed(monkeypatch):
    monkeypatch.setenv("TABLETOP_TEST_MCP_SECRET", "значение")
    spec = fake_spec()._replace(env_keys=("TABLETOP_TEST_MCP_SECRET", "НЕТ_ТАКОЙ_ПЕРЕМЕННОЙ"))
    client = MCPClient(spec)
    assert client.server_environment()["TABLETOP_TEST_MCP_SECRET"] == "значение"
    assert "НЕТ_ТАКОЙ_ПЕРЕМЕННОЙ" not in client.server_environment()


def test_tool_names_are_not_hardcoded_in_the_app():
    """Замена сервера — одна запись реестра: имена его инструментов в коде не встречаются."""
    roots = [Path(__file__).resolve().parents[2] / name for name in ("core", "ui")]
    sources = [path.read_text(encoding="utf-8") for root in roots for path in root.glob("*.py")]
    for name in (
        "bgg_search",
        "bgg_game_details",
        "bgg_top_games",
        "bgg_user_collection",
        # Свой сервер проекта — такой же чужой процесс: его инструменты тоже приходят
        # по протоколу, а не из кода приложения.
        "dnd_sections",
        "dnd_search",
        "dnd_entry",
        "dnd_digest",
        # Планировщик — тоже инструменты сервера: приложение читает его файл состояния,
        # но заданиями управляют модель и ручной вызов, а не код приложения по имени.
        # Имя сводки — schedule_summary, а не schedule_report: последнее совпало бы подстрокой
        # с методом снимка агента `schedule_report()` и уронило бы эту проверку.
        "schedule_add",
        "schedule_list",
        "schedule_run_due",
        "schedule_summary",
    ):
        assert not any(name in source for source in sources)


def test_missing_sdk_reports_the_environment_not_the_server(monkeypatch):
    """Пакета mcp нет в интерпретаторе — это про окружение, а не про сервер.

    Ленивый импорт раньше отдавал голый ModuleNotFoundError строкой отказа подключения, и
    запуск системным Python 3.9 (где пакет не ставится вовсе) читался как «сервер не поднялся».
    """
    import builtins

    real_import = builtins.__import__

    def without_mcp(name, *args, **kwargs):
        if name == "mcp" or name.startswith("mcp."):
            raise ModuleNotFoundError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_mcp)

    with pytest.raises(MCPError) as excinfo:
        MCPClient(fake_spec()).connect()

    message = str(excinfo.value)
    assert "mcp" in message
    assert "3.10" in message
    assert sys.executable in message


# --- вызов инструмента ---


def test_call_tool_returns_text_with_arguments():
    result = MCPClient(fake_spec()).call_tool("fake_echo", {"first": "раз", "second": "два"})
    assert "раз" in result
    assert "два" in result


def test_call_tool_without_arguments():
    assert MCPClient(fake_spec()).call_tool("fake_search", {})


def test_call_unknown_tool_raises_mcp_error():
    with pytest.raises(MCPError) as excinfo:
        MCPClient(fake_spec()).call_tool("нет-такого-инструмента", {})
    assert str(excinfo.value)


def test_call_tool_on_unavailable_server_raises_mcp_error():
    spec = fake_spec()._replace(command="нет-такой-команды-на-диске")
    with pytest.raises(MCPError):
        MCPClient(spec).call_tool("fake_search", {})


def test_call_tool_leaves_no_process_behind():
    """Соединение одноразовое: после вызова процесс сервера не остаётся висеть."""
    before = _fake_server_processes()
    MCPClient(fake_spec()).call_tool("fake_search", {})
    assert _fake_server_processes() == before


def test_tools_carry_input_schemas():
    connection = MCPClient(fake_spec()).connect()
    echo = next(tool for tool in connection.tools if tool.name == "fake_echo")
    assert echo.input_schema["properties"]["first"]["type"] == "string"
    assert echo.input_schema["required"] == ["first"]


def _fake_server_processes() -> int:
    listing = subprocess.run(["ps", "-A", "-o", "command"], capture_output=True, text=True)
    return listing.stdout.count(str(FAKE_SERVER))
