"""Стратегии управления контекстом в реальном приложении: что уходит модели на каждой.

Тесты проверяют не отрисовку, а состав запросов в stub-сервере: модель окна шлёт только
последние сообщения, стратегия фактов добавляет перед вопросом отдельный запрос извлекателя
и блок фактов, ветки собирают запрос из ходов активной ветки.
"""

import json
import time

import pytest

from core import context_strategies
from ui import settings_screen

from . import harness
from .stub_api import answer

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

SETTINGS_MARKER = "↑/↓ — поле, ←/→ — формат и стратегия"


def _roles(payload):
    return [message["role"] for message in payload["messages"]]


def open_settings(session):
    session.wait_for_prompt()
    session.send_line("/settings")
    return session.wait_on_screen(SETTINGS_MARKER)


def close_settings(session):
    """Esc и ожидание ухода панели с экрана (промпт в скроллбэке — ложное ожидание)."""
    session.send_keys(harness.KEY_ESC)
    return session.wait_until_gone(SETTINGS_MARKER)


# Пауза между нажатиями: стрелка — три байта, и при меньшем зазоре читатель клавиш может
# принять начало следующей за одиночный Esc (см. ui/keyboard.py — разбор escape-последовательности).
KEY_PAUSE = 0.12


def go_to_row(session, current: int, row: int) -> int:
    """Доводит курсор экрана настроек до нужной строки и возвращает новую позицию."""
    key = harness.KEY_DOWN if row > current else harness.KEY_UP
    for _ in range(abs(row - current)):
        session.send_key(key, 1)
        time.sleep(KEY_PAUSE)
    return row


def configure(session, strategy=None, window=None) -> None:
    """Ставит стратегию и/или размер окна за одно посещение экрана настроек.

    Строки задаются их нумерацией в `ui/settings_screen`, а не числом нажатий в тесте:
    добавление строки в экран не должно переписывать e2e-сценарий.
    """
    row = open_settings(session) and settings_screen.ROW_FORMAT

    if strategy is not None:
        row = go_to_row(session, row, settings_screen.ROW_STRATEGY)
        index = settings_screen.STRATEGY_VALUES.index(strategy)
        for _ in range(index):
            session.send_key(harness.KEY_RIGHT, 1)
            time.sleep(KEY_PAUSE)

    if window is not None:
        row = go_to_row(session, row, settings_screen.ROW_COMPRESS_AFTER)
        session.send_keys(*[harness.KEY_BACKSPACE] * 5)
        time.sleep(KEY_PAUSE)
        session.send_keys(*[ch.encode() for ch in str(window)])
        session.wait_on_screen(f"Сжатие после (5..50 сообщений): {window}")

    close_settings(session)


def test_window_strategy_sends_only_the_last_messages(app, stub, history_file):
    """Окно 5 сообщений: в запросе последние обмены, суммаризатор не вызывается вовсе."""
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        configure(session, strategy=settings_screen.ContextStrategy.SLIDING_WINDOW, window=5)
        session.wait_for("Стратегия: окно")
        for number in range(1, 5):
            session.send_line(f"Вопрос {number}")
            harness.wait_for_answers(session, number)

        session.send_line("/context")
        session.wait_on_screen("Состояние контекста")

    payload = stub.last_payload()
    # окно 5 сообщений — целыми обменами это два последних обмена плюс новый user-ход:
    # к четвёртому вопросу в логе три обмена, поэтому уходят вопросы 2–4, а первый выпал
    assert _roles(payload) == ["system", "user", "assistant", "user", "assistant", "user"]
    contents = "".join(message["content"] for message in payload["messages"])
    assert "Вопрос 1" not in contents
    assert "Вопрос 2" in contents and "Вопрос 4" in contents
    assert all(
        request["payload"]["messages"][0]["content"] != context_strategies.facts_instruction()
        and request["payload"]["messages"][0]["content"] != harness_summary_instruction()
        for request in stub.requests
    )


def test_facts_strategy_updates_the_block_before_answering(app, stub, history_file):
    """Стратегия фактов: перед вопросом уходит извлекатель, блок попадает в запрос вопроса."""
    facts_json = '{"цель": "собрать ТЗ по Каркассону"}'
    stub.sequence(
        answer(facts_json),              # извлекатель перед вопросом
        answer("Ответ по блоку фактов."),
    )
    with app() as session:
        configure(session, strategy=settings_screen.ContextStrategy.STICKY_FACTS)
        session.wait_for("Стратегия: факты")
        session.send_line("Помоги собрать ТЗ по Каркассону")
        harness.wait_for_answers(session, 1)
        session.wait_for("Факты обновлены")
        session.send_line("/context")
        session.wait_on_screen("Состояние контекста")

    extractor, question = stub.payload_at(0), stub.payload_at(1)
    assert extractor["messages"][0]["content"] == context_strategies.facts_instruction()
    assert "Помоги собрать ТЗ по Каркассону" in extractor["messages"][1]["content"]
    assert _roles(question) == ["system", "system", "user"]
    assert "цель: собрать ТЗ по Каркассону" in question["messages"][1]["content"]

    saved = json.loads(history_file.read_text(encoding="utf-8"))
    assert saved["facts"] == {"цель": "собрать ТЗ по Каркассону"}


def test_branches_panel_switches_the_active_branch(app, stub, history_file):
    """Панель /branches: чекпоинт, новая ветка, возврат — без единого запроса к модели."""
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        configure(session, strategy=settings_screen.ContextStrategy.BRANCHING)
        session.wait_for("Стратегия: ветки")
        session.send_line("Вопрос первой ветки")
        harness.wait_for_answers(session, 1)
        calls_after_first = stub.call_count

        session.send_line("/branches")
        session.wait_on_screen("Ветки диалога")
        session.send_key(b"c", 1)  # чекпоинт
        session.wait_until_gone("Ветки диалога")

        session.send_line("/branches")
        session.wait_on_screen("Ветки диалога")
        session.send_key(b"n", 1)  # ветка от чекпоинта
        session.wait_for("Создана ветка ветка 2")
        session.wait_until_gone("Ветки диалога")

        session.send_line("Вопрос второй ветки")
        harness.wait_for_answers(session, 2)

        session.send_line("/branches")
        session.wait_on_screen("Ветки диалога")
        session.send_key(harness.KEY_UP, 1)
        session.send_key(harness.KEY_ENTER, 1)
        session.wait_until_gone("Ветки диалога")

        session.send_line("Вопрос обратно в первой ветке")
        harness.wait_for_answers(session, 3)

    # панели веток не делали запросов: после установки стратегии — только три вопроса
    assert stub.call_count == calls_after_first + 2
    second_payload = stub.payload_at(1)
    assert "Вопрос первой ветки" in "".join(
        message["content"] for message in second_payload["messages"]
    )
    assert "Вопрос второй ветки" in "".join(
        message["content"] for message in second_payload["messages"]
    )
    third_payload = stub.payload_at(2)
    contents = "".join(message["content"] for message in third_payload["messages"])
    assert "Вопрос обратно в первой ветке" in contents
    assert "Вопрос второй ветки" not in contents  # обмен второй ветки в первую не попал

    saved = json.loads(history_file.read_text(encoding="utf-8"))
    assert [record["question"] for record in saved["dialogues"]] == [
        "Вопрос первой ветки",
        "Вопрос второй ветки",
        "Вопрос обратно в первой ветке",
    ]


def harness_summary_instruction():
    """Инструкция суммаризатора: на стратегиях окна и фактов её в запросах быть не должно."""
    from core import context_compressor

    return context_compressor.summary_instruction()
