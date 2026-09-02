"""Экран /settings в живом терминале.

Здесь проверяется то, что недоступно юнит-тестам редуктора: настоящие escape-последо­
вательности стрелок, cbreak-режим, отрисовка панели через `rich.Live` и — главное — как
изменённые настройки отражаются на следующем запросе к API.
"""

import json

import pytest

from .harness import (
    CTRL_C,
    KEY_BACKSPACE,
    KEY_DOWN,
    KEY_ESC,
    KEY_LEFT,
    KEY_RIGHT,
    KEY_UP,
    assert_snapshot,
)
from .stub_api import answer

pytestmark = [pytest.mark.e2e, pytest.mark.pty]


SETTINGS_MARKER = "Лимит вариантов в списке"


def open_settings(session):
    session.wait_for_prompt()
    session.send_line("/settings")
    return session.wait_on_screen(SETTINGS_MARKER)


def close_settings(session):
    """Esc и ожидание, пока панель действительно уйдёт с экрана.

    Ждать промпт по скроллбэку здесь нельзя: он печатался и до открытия настроек, поэтому
    такое ожидание вернулось бы мгновенно, ещё до того как Live успел стереть панель.
    """
    session.send_keys(KEY_ESC)
    return session.wait_until_gone(SETTINGS_MARKER)


# --- отрисовка и навигация ----------------------------------------------------------------


def test_settings_screen_shows_all_three_rows(app):
    with app() as session:
        screen = open_settings(session)

    assert "Формат ответа" in screen
    assert "Макс. объём" in screen
    assert "Лимит вариантов в списке" in screen
    assert "Esc — выход" in screen


def test_marker_moves_down_between_rows(app):
    """Стрелки действительно доходят до приложения как стрелки, а не как Esc + мусор."""
    with app() as session:
        open_settings(session)
        session.send_keys(KEY_DOWN)
        session.read_for(0.3)
        after_down = session.screen_text()
        assert "➤ Макс. объём" in after_down

        session.send_keys(KEY_DOWN)
        session.read_for(0.3)
        assert "➤ Лимит вариантов" in session.screen_text()


def test_marker_moves_up(app):
    with app() as session:
        open_settings(session)
        session.send_keys(KEY_UP)
        session.read_for(0.3)
        assert "➤ Лимит вариантов" in session.screen_text()


def test_escape_closes_the_screen(app):
    """Одиночный Esc должен закрывать экран, а не приниматься за начало стрелки."""
    with app() as session:
        open_settings(session)
        screen = close_settings(session)
        assert "Настройки" not in screen


# --- изменение настроек -------------------------------------------------------------------


def test_full_settings_walkthrough_changes_the_next_request(app, stub):
    """Полный путь пользователя: формат стрелкой, объём и лимит цифрами, Esc — и запрос."""
    stub.always(answer('```json\n{"name_ru": "Каркассон"}\n```'))
    with app() as session:
        open_settings(session)
        session.send_keys(KEY_RIGHT)  # свободный -> компактный
        session.send_keys(KEY_RIGHT)  # компактный -> JSON
        session.send_keys(KEY_DOWN)
        session.send_keys(KEY_BACKSPACE, KEY_BACKSPACE, KEY_BACKSPACE)
        session.send_keys(b"5", b"0")
        session.send_keys(KEY_DOWN)
        session.send_keys(KEY_BACKSPACE)
        session.send_keys(b"6")
        session.send_keys(KEY_ESC)

        session.wait_for("Формат: JSON")
        session.wait_for("Объём: 50 слов")
        session.wait_for("Лимит списка: 6")

        session.send_line("Расскажи про Каркассон")
        session.wait_for("name_ru")

    payload = stub.last_payload()
    assert "Формат ответа: JSON" in payload["messages"][0]["content"]
    assert "не более 50 слов" in payload["messages"][1]["content"]
    assert "не более 6 вариантов" in payload["messages"][1]["content"]
    assert payload["max_tokens"] == 50 * 4 + 50
    assert "stop" not in payload


def test_format_switches_the_system_message(app, stub):
    with app() as session:
        open_settings(session)
        session.send_keys(KEY_LEFT)  # свободный -> JSON (по кругу назад)
        close_settings(session)
        session.wait_for("Формат: JSON")
        session.send_line("Расскажи про Catan")
        session.wait_for("Ответ stub-сервера")

    assert "Формат ответа: JSON" in stub.system_messages()[0]


def test_free_format_sends_no_format_instruction(app, stub):
    """По умолчанию формат свободный — в системном сообщении только базовый промпт."""
    with app() as session:
        session.ask("Расскажи про Catan", "Ответ stub-сервера")

    system = stub.system_messages()[0]
    assert "Tabletop AI Assistant" in system
    assert "Формат ответа:" not in system


def test_invalid_value_is_reported_and_previous_value_kept(app, stub):
    with app() as session:
        open_settings(session)
        session.send_keys(KEY_DOWN)
        session.send_keys(KEY_BACKSPACE, KEY_BACKSPACE, KEY_BACKSPACE)
        session.send_keys(b"9", b"9", b"9")
        session.send_keys(KEY_ESC)

        session.wait_for("Значение должно быть в диапазоне 10..500")
        session.wait_for("Объём: 200 слов")

        session.send_line("Вопрос")
        session.wait_for("Ответ stub-сервера")

    assert "не более 200 слов" in stub.user_messages()[0]


def test_emptied_field_is_reported(app):
    with app() as session:
        open_settings(session)
        session.send_keys(KEY_DOWN)
        session.send_keys(*[KEY_BACKSPACE] * 5)
        session.send_keys(KEY_ESC)

        session.wait_for("введите число слов")
        session.wait_for("Объём: 200 слов")


def test_settings_survive_and_apply_to_every_later_question(app, stub):
    with app() as session:
        open_settings(session)
        session.send_keys(KEY_DOWN)
        session.send_keys(KEY_BACKSPACE, KEY_BACKSPACE, KEY_BACKSPACE)
        session.send_keys(b"3", b"0")
        close_settings(session)
        session.wait_for("Объём: 30 слов")

        session.ask("Первый вопрос", "Ответ stub-сервера")
        session.wait_for("Диалогов за сессию: 1")
        session.send_line("Второй вопрос")
        session.wait_for("Диалогов за сессию: 2")

    assert all("не более 30 слов" in message for message in stub.user_messages())


def test_settings_screen_can_be_reopened(app):
    """Терминал корректно возвращается в строчный режим — экран открывается повторно."""
    with app() as session:
        open_settings(session)
        close_settings(session)
        open_settings(session)
        close_settings(session)
        session.send_line("/exit")
        assert session.wait_exit() == 0


def test_settings_screen_does_not_stay_in_the_log(app):
    """Панель настроек рисуется transient — после выхода она не засоряет экран."""
    with app() as session:
        open_settings(session)
        screen = close_settings(session)
        assert "Формат ответа" not in screen
        assert "Введите вопрос" in screen


# --- предупреждение о JSON --------------------------------------------------------------------


def test_json_format_warns_about_a_prose_answer(app, stub):
    stub.always(answer("Каркассон — отличная игра, но это совсем не JSON."))
    with app() as session:
        open_settings(session)
        session.send_keys(KEY_LEFT)  # -> JSON
        close_settings(session)
        session.wait_for("Формат: JSON")
        session.send_line("Расскажи про Каркассон")
        session.wait_for("совсем не JSON")
        session.wait_for("Модель не вернула валидный JSON")


def test_json_format_accepts_a_valid_answer(app, stub):
    stub.always(answer('```json\n{"name_ru": "Каркассон", "genre": "тайлы"}\n```'))
    with app() as session:
        open_settings(session)
        session.send_keys(KEY_LEFT)
        close_settings(session)
        session.wait_for("Формат: JSON")
        session.send_line("Расскажи про Каркассон")
        session.wait_for("name_ru")
        session.read_for(0.5)
        assert not session.contains("Модель не вернула валидный JSON")


def test_invalid_json_is_still_saved_to_history(app, stub, history_file):
    """Предупреждение не отменяет ответ: он показан и сохранён."""
    stub.always(answer("Это не JSON."))
    with app() as session:
        open_settings(session)
        session.send_keys(KEY_LEFT)
        close_settings(session)
        session.wait_for("Формат: JSON")
        session.send_line("Вопрос")
        session.wait_for("Модель не вернула валидный JSON")
        session.send_line("/exit")
        session.wait_exit()

    saved = json.loads(history_file.read_text(encoding="utf-8"))
    assert saved[0]["answer"] == "Это не JSON."
