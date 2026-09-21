"""Команда /mcp в живом приложении: подключение, список инструментов, отказ, ноль запросов.

Сервер здесь — фейковый локальный (`tests/fake_mcp_server.py`, подставляется фикстурой `app`):
прогон не поднимает сервер из реестра и не ходит в сеть.
"""

import sys

import pytest

from .harness import FAKE_MCP_SERVER
from .stub_api import answer

pytestmark = [pytest.mark.e2e, pytest.mark.pty]


def test_mcp_report_shows_server_protocol_and_tools(app, stub):
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/mcp")
        text = session.wait_for("fake_details")

    assert "фейковый-сервер" in text
    assert "9.9.9" in text
    assert "2025-06-18" in text
    assert "fake_search" in text
    assert "Поиск по фейковому каталогу" in text
    # Подключение к MCP — не запрос к модели.
    assert stub.call_count == 0


def test_mcp_is_listed_in_the_commands_panel(app, stub):
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/commands")
        text = session.wait_on_screen("подключение к MCP-серверу")

    assert "/mcp" in text
    assert stub.call_count == 0


def test_mcp_spends_no_tokens(app, stub):
    stub.always(answer("Берите Каркассон."))
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/mcp")
        session.wait_for("fake_details")
        session.send_line("/usage")
        text = session.wait_for("Сессия: запросов")

    assert stub.call_count == 0
    assert "Последний запрос: пока не было запросов" in text
    assert "Сессия: запросов 0" in text


def test_unavailable_server_prints_the_reason_and_session_continues(app, stub):
    stub.always(answer("Берите Каркассон."))
    with app(mcp_command="нет-такой-команды-на-диске", mcp_args="") as session:
        session.wait_for_prompt()
        session.send_line("/mcp")
        session.wait_for("Не удалось подключиться")
        # Сессия жива: следующий вопрос обрабатывается как обычно.
        session.ask("Что взять на вечер?", "Берите Каркассон.")

    assert stub.call_count == 1


def test_real_registry_server_is_not_started_in_the_run(app, stub):
    """Изоляция: команда запускает именно фейковый сервер прогона."""
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/mcp")
        text = session.wait_for("fake_details")

    assert "fake_mcp_server.py" in text
    assert "npx" not in text
    assert sys.executable.split("/")[-1] in text or str(FAKE_MCP_SERVER.name) in text
