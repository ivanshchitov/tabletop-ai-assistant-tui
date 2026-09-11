"""Команда /commands против stub API: панель списка и выполнение выбранной команды.

Enter в панели выполняет команду напрямую — тот же обработчик, что при ручном наборе;
поведение не зависит от возможностей построчного редактирования терминала.
"""

import json

from . import harness
from .stub_api import answer


def _wait_panel_open(session) -> None:
    """Ждём отрисованный кадр панели (хинт виден только внутри Live-панели)."""
    session.wait_on_screen("Enter — выполнить")


def _wait_panel_closed(session) -> None:
    """Transient-панель erase'ится при закрытии — ждём исчезновения её хинта."""
    session.wait_until_gone("↑/↓ — выбор, Enter — выполнить")


def test_panel_lists_commands_and_esc_makes_no_requests(app, stub, history_file):
    session = app()
    session.wait_for_prompt()
    session.send_line("/commands")
    session.wait_on_screen("Команды")
    session.wait_on_screen("/exit")
    session.wait_on_screen("/commands — показать эту панель")
    session.wait_on_screen("/settings — настройки формата и объёма ответа")
    session.wait_on_screen("/clear — очистить историю диалога")
    session.wait_on_screen("/logictask — решить логическую задачу выбранной стратегией")
    _wait_panel_open(session)

    session.send_key(harness.KEY_ESC, 1)
    _wait_panel_closed(session)
    session.wait_for_prompt()
    assert stub.call_count == 0

    session.send_line("/exit")
    session.wait_exit()
    assert not history_file.exists() or json.loads(history_file.read_text(encoding="utf-8")) == {
        "summary": None,
        "summary_covers": 0,
        "facts": {},
        "dialogues": [],
    }


def test_enter_runs_selected_command(app, stub, history_file):
    session = app()
    session.wait_for_prompt()
    session.send_line("/commands")
    _wait_panel_open(session)

    # ↓↓↓↓ — выбор /clear; Enter выполняет команду прямо из панели.
    session.send_key(harness.KEY_DOWN, 4)
    session.send_key(harness.KEY_ENTER, 1)
    _wait_panel_closed(session)

    session.wait_for("История диалога очищена.")
    assert stub.call_count == 0

    session.send_line("/exit")
    session.wait_exit()
    assert not history_file.exists() or json.loads(history_file.read_text(encoding="utf-8")) == {
        "summary": None,
        "summary_covers": 0,
        "facts": {},
        "dialogues": [],
    }


def test_settings_selection_opens_settings_screen(app, stub):
    session = app()
    session.wait_for_prompt()
    session.send_line("/commands")
    _wait_panel_open(session)

    # ↓↓ — выбор /settings; Enter открывает экран настроек.
    session.send_key(harness.KEY_DOWN, 2)
    session.send_key(harness.KEY_ENTER, 1)
    session.wait_until_gone("↑/↓ — выбор, Enter — выполнить")
    session.wait_on_screen("Настройки")
    session.wait_on_screen("Формат ответа")
    assert stub.call_count == 0

    session.send_key(harness.KEY_ESC, 1)  # выходим с экрана настроек
    session.wait_until_gone("Формат ответа")
    # После закрытия raw-mode-экрана даём приложению дойти до input(), иначе первый
    # байт может пропасть при переключении терминала обратно в канонический режим.
    import time

    time.sleep(0.5)
    session.send_line("/exit")
    session.wait_exit()
    assert stub.call_count == 0


def test_exit_selection_terminates_app(app, stub, history_file):
    session = app()
    session.wait_for_prompt()
    session.send_line("/commands")
    _wait_panel_open(session)

    session.send_key(harness.KEY_ENTER, 1)  # первая строка панели — /exit
    session.wait_exit()
    assert stub.call_count == 0
    assert not history_file.exists() or json.loads(history_file.read_text(encoding="utf-8")) == {
        "summary": None,
        "summary_covers": 0,
        "facts": {},
        "dialogues": [],
    }


def test_status_bar_hint_lists_only_exit_and_commands(app, stub):
    session = app()
    session.wait_for_prompt()
    session.wait_for("Команды: /exit, /commands")
    status_line = next(
        line for line in session.scrollback().splitlines() if "Команды:" in line
    )
    assert "/settings" not in status_line
    assert "/clear" not in status_line
    assert "/logictask" not in status_line

    session.send_line("/exit")
    session.wait_exit()


def test_usage_command_reports_tokens_without_api_calls(app, stub, history_file):
    stub.always(answer("Короткий ответ stub-модели."))
    session = app()
    session.wait_for_prompt()
    session.send_line("Вопрос про Каркассон")
    session.wait_for("⏱")
    session.send_line("/usage")
    session.wait_on_screen("Учёт токенов")
    session.wait_on_screen("Последний запрос: токены 50+100=150")
    session.wait_on_screen("Сессия: запросов 1")
    session.wait_on_screen("Всего диалога (файл истории): запросов 1")
    session.wait_on_screen("1 обменов в стеке")
    assert stub.call_count == 1  # отчёт не обращается к модели

    # отчёт не пишется в историю
    saved = json.loads(history_file.read_text(encoding="utf-8"))
    assert len(saved["dialogues"]) == 1 and "usage" in saved["dialogues"][0]

    session.send_line("/exit")
    session.wait_exit()


def test_empty_truncated_answer_warns_and_session_continues(app, stub):
    stub.always(answer("", finish_reason="length"))
    session = app()
    session.wait_for_prompt()
    session.send_line("Невозможно сложный вопрос")
    session.wait_on_screen("исчерпала бюджет")
    assert stub.call_count == 1

    # сессия продолжается: следующий вопрос обрабатывается нормально
    stub.always(answer("Ответ после сбоя."))
    session.send_line("Ещё вопрос")
    session.wait_on_screen("Ответ после сбоя.")
    session.send_line("/exit")
    session.wait_exit()
