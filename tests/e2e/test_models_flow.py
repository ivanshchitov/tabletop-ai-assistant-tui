"""Команда /models против stub API: панель выбора модели и её применение к запросам."""

import json

from . import harness

DEFAULT_MODEL = "deepseek-v4-flash"


def _wait_panel_open(session) -> None:
    """Хинт «Enter — применить» виден только внутри Live-панели /models."""
    session.wait_on_screen("Enter — применить")


def _wait_panel_closed(session) -> None:
    """Transient-панель erase'ится при закрытии — ждём исчезновения её хинта."""
    session.wait_until_gone("Enter — применить")


def test_panel_lists_models_with_current_marked(app, stub):
    session = app()
    session.wait_for_prompt()
    session.send_line("/models")
    _wait_panel_open(session)

    session.wait_on_screen("deepseek-v4-flash (текущая)")
    for model in ("deepseek-v4-pro", "kimi-k2.5", "glm-5.1", "mimo-v2.5-free"):
        session.wait_on_screen(model)
    assert stub.call_count == 0

    session.send_key(harness.KEY_ESC, 1)
    _wait_panel_closed(session)
    session.wait_for_prompt()
    assert stub.call_count == 0

    session.send_line("/exit")
    session.wait_exit()


def test_select_model_then_question_uses_it(app, stub, history_file):
    session = app()
    session.wait_for_prompt()
    session.send_line("/models")
    _wait_panel_open(session)

    # ↓ — с дефолтной deepseek-v4-flash на deepseek-v4-pro; Enter применяет.
    session.send_key(harness.KEY_DOWN, 1)
    session.send_key(harness.KEY_ENTER, 1)
    session.wait_for("Модель: deepseek-v4-pro")  # статус-бар после закрытия панели
    session.wait_for_prompt()
    assert stub.call_count == 0

    session.send_line("Правила Каркассона?")
    session.wait_for("Ответ stub-сервера.")
    assert stub.last_payload()["model"] == "deepseek-v4-pro"

    session.send_line("/exit")
    session.wait_exit()
    assert json.loads(history_file.read_text(encoding="utf-8"))[0]["question"] == (
        "Правила Каркассона?"
    )


def test_esc_keeps_default_model(app, stub):
    session = app()
    session.wait_for_prompt()
    session.send_line("/models")
    _wait_panel_open(session)

    session.send_key(harness.KEY_DOWN, 2)
    session.send_key(harness.KEY_ESC, 1)
    _wait_panel_closed(session)
    session.wait_for("Модель: " + DEFAULT_MODEL)
    session.wait_for_prompt()
    assert stub.call_count == 0

    session.send_line("Вопрос")
    session.wait_for("Ответ stub-сервера.")
    assert stub.last_payload()["model"] == DEFAULT_MODEL

    session.send_line("/exit")
    session.wait_exit()


def test_commands_panel_lists_models_row(app, stub):
    session = app()
    session.wait_for_prompt()
    session.send_line("/commands")
    session.wait_on_screen("/models — выбрать модель для ответов")
    session.wait_on_screen("Enter — выполнить")
    assert stub.call_count == 0

    session.send_key(harness.KEY_ESC, 1)
    session.wait_until_gone("Enter — выполнить")
    session.wait_for_prompt()

    session.send_line("/exit")
    session.wait_exit()
