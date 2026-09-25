"""Команда /tool и автоматический вызов инструмента в живом приложении.

Сервер — фейковый локальный (`tests/fake_mcp_server.py`, подставляется фикстурой `app`), модель —
stub: прогон не поднимает серверы реестра и не ходит в сеть. Автовызов включается явно
(`auto_tools=True`): по умолчанию прогон его выключает, иначе каждый тест платил бы
вспомогательный запрос выбора инструмента.
"""

import json

import pytest

from .harness import FAKE_MCP_SERVER
from .stub_api import answer

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

CHOICE = '{"server": "переопределён окружением", "tool": "fake_echo", "arguments": {"first": "гоблин"}}'
NO_CHOICE = '{"tool": null}'


def test_tool_list_prints_tools_with_parameters(app, stub):
    with app() as session:
        session.wait_for("MCP: 1/1")
        session.wait_for_prompt()
        session.send_line("/tool list")
        text = session.wait_for("fake_echo")

    assert "Первый аргумент" in text
    assert "обязательный" in text
    # Перечень строится по снимкам запуска: к модели не обращается.
    assert stub.call_count == 0


def test_manual_call_prints_the_result(app, stub):
    with app() as session:
        session.wait_for("MCP: 1/1")
        session.wait_for_prompt()
        session.send_line("/tool call fake_echo first=гоблин")
        text = session.wait_for("фейковый вызов")

    assert "first=гоблин" in text
    assert stub.call_count == 0


def test_manual_call_of_an_unknown_tool_keeps_the_session(app, stub):
    with app() as session:
        session.wait_for("MCP: 1/1")
        session.wait_for_prompt()
        session.send_line("/tool call нет-такого")
        session.wait_for("Вызов не удался")
        session.send_line("/tool list")
        text = session.wait_for("fake_search")

    assert "fake_echo" in text


def test_tool_is_listed_in_the_commands_panel(app, stub):
    with app() as session:
        session.wait_for("MCP: 1/1")
        session.wait_for_prompt()
        session.send_line("/commands")
        text = session.wait_on_screen("инструменты MCP")

    assert "/tool" in text


def test_automatic_call_puts_the_result_into_the_request(app, stub):
    stub.sequence(answer(CHOICE), answer("Ответ с учётом данных инструмента."))
    with app(auto_tools=True) as session:
        session.wait_for("MCP: 1/1")
        session.wait_for_prompt()
        session.send_line("Сколько хитов у гоблина?")
        text = session.wait_for("Токены:")

    assert "🔧" in text
    assert "fake_echo" in text
    # В запрос вопроса ушёл результат вызова отдельным системным сообщением.
    systems = [m["content"] for m in stub.payload_at(1)["messages"] if m["role"] == "system"]
    assert any("фейковый вызов fake_echo" in content for content in systems)
    assert stub.call_count == 2


def test_model_may_decide_that_no_tool_is_needed(app, stub):
    stub.sequence(answer(NO_CHOICE), answer("Ответ без инструмента."))
    with app(auto_tools=True) as session:
        session.wait_for("MCP: 1/1")
        session.wait_for_prompt()
        session.send_line("Вопрос без справочника")
        text = session.wait_for("Токены:")

    assert "Ответ без инструмента." in text
    systems = [m["content"] for m in stub.payload_at(1)["messages"] if m["role"] == "system"]
    assert not any("фейковый вызов" in content for content in systems)


def test_auto_off_stops_the_choice_request(app, stub):
    stub.always(answer("Ответ без выбора инструмента."))
    with app(auto_tools=True) as session:
        session.wait_for("MCP: 1/1")
        session.wait_for_prompt()
        session.send_line("/tool auto off")
        session.wait_for("выключен")
        session.send_line("Вопрос про настолки")
        session.wait_for("Токены:")

    assert stub.call_count == 1


def test_failed_choice_does_not_block_the_answer(app, stub):
    stub.sequence(answer("совершенно не JSON"), answer("Ответ несмотря на сбой."))
    with app(auto_tools=True) as session:
        session.wait_for("MCP: 1/1")
        session.wait_for_prompt()
        session.send_line("Вопрос про настолки")
        text = session.wait_for("Токены:")

    assert "Инструмент не вызван" in text
    assert "Ответ несмотря на сбой." in text


def test_unavailable_server_does_not_block_the_answer(app, stub):
    """Сервер не поднимается — инструментов нет, вопрос уходит как обычно, одним запросом."""
    stub.always(answer("Ответ без инструментов."))
    with app(auto_tools=True, mcp_command="нет-такой-команды-на-диске", mcp_args="") as session:
        session.wait_for("MCP: 0/1")
        session.wait_for_prompt()
        session.send_line("Вопрос про настолки")
        session.wait_for("Токены:")

    assert stub.call_count == 1


def test_tool_lines_are_not_written_to_history(app, stub, history_file):
    stub.sequence(answer(CHOICE), answer("Ответ с данными инструмента."))
    with app(auto_tools=True) as session:
        session.wait_for("MCP: 1/1")
        session.wait_for_prompt()
        session.send_line("Сколько хитов у гоблина?")
        session.wait_for("Токены:")
        session.send_line("/exit")
        session.wait_exit()

    stored = json.loads(history_file.read_text(encoding="utf-8"))
    assert "fake_echo" not in json.dumps(stored, ensure_ascii=False)
    assert stored["dialogues"][-1]["answer"] == "Ответ с данными инструмента."


# --- цепочка инструментов (день 19) ---


CHAIN = json.dumps(
    {
        "steps": [
            {"tool": "fake_search", "arguments": {"query": "огонь"}},
            {"tool": "fake_echo", "arguments": {"first": "$1"}},
            {"tool": "fake_echo", "arguments": {"first": "$2", "second": "fire-spells"}},
        ]
    },
    ensure_ascii=False,
)
STEP_1 = "фейковый вызов fake_search(query=огонь)"
STEP_2 = f"фейковый вызов fake_echo(first={STEP_1})"
STEP_3 = f"фейковый вызов fake_echo(first={STEP_2}, second=fire-spells)"


def test_chain_runs_automatically_and_passes_data(app, stub, history_file):
    """Три шага по одному запросу выбора; вход каждого шага — дословно выход предыдущего."""
    stub.sequence(answer(CHAIN), answer("Сводка сохранена."))
    with app(auto_tools=True) as session:
        session.wait_for("MCP: 1/1")
        session.wait_for_prompt()
        session.send_line("Найди заклинания огня, сведи и сохрани в файл")
        text = session.wait_for("Токены:")
        session.send_line("/exit")
        session.wait_exit()

    # Строки журнала длиннее ширины терминала и переносятся — сверяем по тексту без переносов.
    flat = " ".join(text.split())
    assert "🔧 1/3" in flat and "🔧 2/3" in flat and "🔧 3/3" in flat
    # Объёмы в журнале: шаг 2 получил ровно столько, сколько вернул шаг 1, и так далее.
    assert f"first ← шаг 1: {len(STEP_1)} симв." in flat
    assert f"first ← шаг 2: {len(STEP_2)} симв." in flat
    assert f"→ {len(STEP_3)} симв." in flat
    # Один запрос выбора плюс вопрос: шаги цепочки к модели не обращаются.
    assert stub.call_count == 2
    systems = [m["content"] for m in stub.payload_at(1)["messages"] if m["role"] == "system"]
    chain_message = next(content for content in systems if "Шаг 1" in content)
    # Эхо-сервер вернул то, что получил: результат шага 3 содержит данные шага 1 без искажений.
    assert STEP_3 in chain_message
    assert "first=← шаг 2" in chain_message

    stored = json.loads(history_file.read_text(encoding="utf-8"))
    assert "fake_search" not in json.dumps(stored, ensure_ascii=False)


# --- оркестрация нескольких серверов (день 20) ---


FIRST = "переопределён окружением 1"
SECOND = "переопределён окружением 2"
FLOW = [
    json.dumps({"steps": [{"tool": "fake_search", "arguments": {"query": "огонь"}}], "more": True}, ensure_ascii=False),
    # Модель назвала не тот сервер: маршрут исправляется по каталогу.
    json.dumps(
        {"steps": [{"server": FIRST, "tool": "fake_facts", "arguments": {"name": "гоблин"}}], "more": True},
        ensure_ascii=False,
    ),
    json.dumps({"steps": [{"tool": "fake_note", "arguments": {"text": "$2"}}]}, ensure_ascii=False),
]
FLOW_STEP_1 = "фейковый вызов fake_search(query=огонь)"
FLOW_STEP_2 = "фейковый вызов fake_facts(name=гоблин)"
FLOW_STEP_3 = f"фейковый вызов fake_note(text={FLOW_STEP_2})"


def test_flow_runs_rounds_across_two_servers(app, stub):
    """Три раунда выбора, два сервера: порядок вызовов, маршрут и передача данных между раундами."""
    stub.sequence(*(answer(text) for text in FLOW), answer("Итог по флоу."))
    with app(auto_tools=True, mcp_args=f"{FAKE_MCP_SERVER} | {FAKE_MCP_SERVER} --second") as session:
        session.wait_for("MCP: 2/2")
        session.wait_for_prompt()
        session.send_line("Найди, узнай подробности о найденном и запиши заметку")
        text = session.wait_for("Токены:")
        session.send_line("/tool flow")
        report = session.wait_for("Итог: модель завершила флоу")
        session.send_line("/exit")
        session.wait_exit()

    flat = " ".join(text.split())
    # Порядок и серверы: раунд 1 — первый сервер, раунды 2 и 3 — второй.
    assert flat.index(f"🔧 р1 · 1/3 {FIRST}.fake_search") < flat.index(f"🔧 р2 · 2/3 {SECOND}.fake_facts")
    assert flat.index(f"🔧 р2 · 2/3 {SECOND}.fake_facts") < flat.index(f"🔧 р3 · 3/3 {SECOND}.fake_note")
    assert f"маршрут: {FIRST} → {SECOND}" in flat
    assert f"text ← шаг 2: {len(FLOW_STEP_2)} симв." in flat
    # Три запроса выбора и вопрос; второй выбор видел результат шага 1, третий — шага 2.
    assert stub.call_count == 4
    assert FLOW_STEP_1 in stub.payload_at(1)["messages"][-1]["content"]
    assert FLOW_STEP_2 in stub.payload_at(2)["messages"][-1]["content"]
    systems = [m["content"] for m in stub.payload_at(3)["messages"] if m["role"] == "system"]
    assert any(FLOW_STEP_3 in content for content in systems)
    flat_report = " ".join(report.split())
    assert "Раундов: 3, запросов выбора: 3, шагов: 3" in flat_report
    assert f"Серверы: {FIRST}, {SECOND}" in flat_report
