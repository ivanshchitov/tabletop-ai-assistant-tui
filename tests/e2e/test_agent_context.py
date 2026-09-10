"""Контекст сессии в запросах: агент пересылает стек сообщений, /clear и /logictask его не путают."""

import json
import time

import pytest

from core import context_compressor

from . import harness
from .stub_api import Reply, answer

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

def _roles(payload):
    return [m["role"] for m in payload["messages"]]

def test_second_question_carries_first_exchange(app, stub):
    stub.always(answer("Первый ответ stub-модели."))
    with app() as session:
        session.ask("Первый вопрос", "Первый ответ stub-модели.")
        session.send_line("Второй вопрос")
        harness.wait_for_answers(session, 2)

    payload = stub.payload_at(1)
    assert _roles(payload) == ["system", "user", "assistant", "user"]
    assert "Первый вопрос" in payload["messages"][1]["content"]
    assert payload["messages"][2]["content"] == "Первый ответ stub-модели."
    assert "Второй вопрос" in payload["messages"][3]["content"]

def test_first_question_has_no_context(app, stub):
    with app() as session:
        session.ask("Вопрос", "Ответ stub-сервера.")

    assert _roles(stub.last_payload()) == ["system", "user"]

def test_clear_resets_the_conversation_context(app, stub):
    stub.always(answer("Ответ, который сотрут."))
    with app() as session:
        session.ask("Вопрос до очистки", "Ответ, который сотрут.")
        session.send_line("/clear")
        session.wait_for("История диалога очищена.")
        session.ask("Вопрос после очистки", "Ответ, который сотрут.")
        harness.wait_for_answers(session, 2)

    assert stub.last_payload()["messages"][1]["content"].startswith(
        "Вопрос пользователя: Вопрос после очистки"
    )

def test_logictask_does_not_change_the_conversation_context(app, stub):
    stub.always(answer("Прямой ответ stub-модели."))
    with app() as session:
        session.ask("Обычный вопрос", "Прямой ответ stub-модели.")

        session.send_line("/logictask")
        session.wait_on_screen("Выберите стратегию")
        session.send_key(harness.KEY_ENTER, 1)  # стратегия 1 уже выбрана
        session.wait_for("Стратегия 1: Прямой ответ")
        session.wait_for("Токены: 50+100=150")
        session.wait_for_prompt()

        session.send_line("Следующий вопрос")
        harness.wait_for_answers(session, 3)

    # вызов прогона (запрос 2) — ровно два сообщения, вне стека
    assert _roles(stub.payload_at(1)) == ["system", "user"]
    # обычный вопрос после прогона — тот же состав контекста, что и до него
    payload = stub.payload_at(2)
    assert _roles(payload) == ["system", "user", "assistant", "user"]
    assert "Обычный вопрос" in payload["messages"][1]["content"]

def test_restarted_session_carries_restored_context(app, stub, history_file):
    stub.always(answer("Ответ первой сессии."))
    with app() as session:
        session.ask("Вопрос в первой сессии", "Ответ первой сессии.")

    stub.always(answer("Ответ второй сессии."))
    with app() as session:
        session.wait_for("Вопрос в первой сессии")  # реплей виден на экране
        session.ask("Вопрос во второй сессии", "Ответ второй сессии.")

    # контекст пережил перезапуск: запрос второй сессии несёт прошлый обмен
    payload = stub.payload_at(1)
    assert _roles(payload) == ["system", "user", "assistant", "user"]
    assert "Вопрос в первой сессии" in payload["messages"][1]["content"]
    assert payload["messages"][2]["content"] == "Ответ первой сессии."
    assert "Вопрос во второй сессии" in payload["messages"][3]["content"]

def test_clear_after_restart_resets_restored_context(app, stub, history_file):
    stub.always(answer("Ответ первой сессии."))
    with app() as session:
        session.ask("Вопрос в первой сессии", "Ответ первой сессии.")

    stub.always(answer("Ответ второй сессии."))
    with app() as session:
        session.wait_for("Вопрос в первой сессии")
        session.send_line("/clear")
        session.wait_for("История диалога очищена.")
        session.ask("Вопрос во второй сессии", "Ответ второй сессии.")

    payload = stub.last_payload()
    assert _roles(payload) == ["system", "user"]
    assert "Вопрос в первой сессии" not in "".join(m["content"] for m in payload["messages"])


def open_settings(session):
    session.wait_for_prompt()
    session.send_line("/settings")
    session.wait_on_screen("Настройки")


def close_settings(session):
    session.send_key(harness.KEY_ESC, 1)
    session.wait_until_gone("Настройки")
    time.sleep(0.05)



def set_compress_after(session, value: str) -> None:
    """Порог сжатия — строка 4: ↓×4, стереть, набрать значение, Esc."""
    open_settings(session)
    for _ in range(4):
        session.send_key(harness.KEY_DOWN, 1)
        time.sleep(0.02)
    for _ in range(4):
        session.send_key(harness.KEY_BACKSPACE, 1)
        time.sleep(0.02)
    for ch in value:
        session.send_key(ch.encode(), 1)
        time.sleep(0.02)
    session.wait_on_screen(f"Сжатие после (5..50 сообщений): {value}")
    close_settings(session)


def test_threshold_compression_reaches_the_model(app, stub, history_file):
    """Порог 5 сообщений: после 3 обменов суммаризатор сворачивает старое в резюме."""
    stub.sequence(
        answer("Первый ответ."),
        answer("Второй ответ."),
        answer("Третий ответ."),
        answer("РЕЗЮМЕ ДИАЛОГА."),
        answer("Четвёртый ответ."),
    )
    with app() as session:
        set_compress_after(session, "5")
        session.ask("Вопрос один", "Первый ответ.")
        harness.wait_for_answers(session, 1)
        session.ask("Вопрос два", "Второй ответ.")
        harness.wait_for_answers(session, 2)
        session.ask("Вопрос три", "Третий ответ.")
        harness.wait_for_answers(session, 3)

        session.wait_for_prompt()
        session.send_line("Вопрос четыре")
        harness.wait_for_answers(session, 4)
        session.wait_for("Контекст сжат: 4 сообщений (2 обменов) → резюме")

    assert stub.call_count == 5
    summary_payload = stub.payload_at(3)
    assert summary_payload["messages"][0]["content"] == (
        context_compressor.summary_instruction()
    )
    summary_user = summary_payload["messages"][1]["content"]
    assert "Вопрос один" in summary_user and "Вопрос два" in summary_user
    assert "Вопрос три" not in summary_user
    question_payload = stub.payload_at(4)
    roles = _roles(question_payload)
    assert roles == ["system", "system", "user", "assistant", "user"]
    assert "РЕЗЮМЕ ДИАЛОГА." in question_payload["messages"][1]["content"]
    assert "Вопрос три" in question_payload["messages"][2]["content"]
    assert "Вопрос один" not in "".join(m["content"] for m in question_payload["messages"])
    saved = json.loads(history_file.read_text(encoding="utf-8"))
    assert saved["summary"] == "РЕЗЮМЕ ДИАЛОГА."
    assert saved["summary_covers"] == 2
    assert len(saved["dialogues"]) == 4


def test_restart_reuses_summary_without_summarizer(app, stub, history_file):
    """Резюме хранится в файле: рестарт поднимает его без запроса суммаризатора."""
    stub.sequence(
        answer("Первый ответ."),
        answer("Второй ответ."),
        answer("Третий ответ."),
        answer("РЕЗЮМЕ ДИАЛОГА."),
        answer("Четвёртый ответ."),
    )
    with app() as session:
        set_compress_after(session, "5")
        session.ask("Вопрос один", "Первый ответ.")
        harness.wait_for_answers(session, 1)
        session.ask("Вопрос два", "Второй ответ.")
        harness.wait_for_answers(session, 2)
        session.ask("Вопрос три", "Третий ответ.")
        harness.wait_for_answers(session, 3)
        session.wait_for_prompt()
        session.send_line("Вопрос четыре")
        harness.wait_for_answers(session, 4)
        session.send_line("/exit")
        session.wait_exit()

    stub.reset()
    stub.always(answer("Пятый ответ."))
    with app() as session:
        session.wait_for("Вопрос один")  # реплей истории виден
        session.send_line("Вопрос пять")
        harness.wait_for_answers(session, 1)
        session.read_for(0.3)

    assert "Контекст сжат" not in session.scrollback()  # рестарт без суммаризатора

    assert stub.call_count == 1  # суммаризатор не потребовался
    payload = stub.payload_at(0)
    roles = _roles(payload)
    assert roles[0] == "system" and roles[1] == "system"
    assert "РЕЗЮМЕ ДИАЛОГА." in payload["messages"][1]["content"]
    contents = "".join(m["content"] for m in payload["messages"])
    assert "Вопрос один" not in contents and "Вопрос два" not in contents
    assert "Вопрос три" in contents and "Вопрос четыре" in contents


def test_clear_wipes_summary_too(app, stub, history_file):
    stub.sequence(
        answer("Первый ответ."),
        answer("Второй ответ."),
        answer("Третий ответ."),
        answer("РЕЗЮМЕ ДИАЛОГА."),
        answer("Четвёртый ответ."),
    )
    with app() as session:
        set_compress_after(session, "5")
        session.ask("Вопрос один", "Первый ответ.")
        harness.wait_for_answers(session, 1)
        session.ask("Вопрос два", "Второй ответ.")
        harness.wait_for_answers(session, 2)
        session.ask("Вопрос три", "Третий ответ.")
        harness.wait_for_answers(session, 3)
        session.wait_for_prompt()
        session.send_line("Вопрос четыре")
        harness.wait_for_answers(session, 4)
        session.send_line("/clear")
        session.wait_for("История диалога очищена.")
        session.send_line("/exit")
        session.wait_exit()

    assert stub.call_count == 5  # сжатие выполнено один раз, до /clear
    data = json.loads(history_file.read_text(encoding="utf-8"))
    assert data["summary"] is None and data["summary_covers"] == 0


def test_settings_screen_shows_six_rows(app):
    with app() as session:
        open_settings(session)
        screen = session.screen_text()

    for row in (
        "Формат ответа",
        "Макс. объём",
        "Лимит вариантов",
        "Температура",
        "Сжатие после",
        "Потолок контекста",
    ):
        assert row in screen


def test_no_dialogue_counter_in_status_bar(app, stub):
    stub.always(answer("Ответ."))
    with app() as session:
        session.ask("Вопрос", "Ответ.")
        harness.wait_for_answers(session, 1)

    assert "Диалогов за сессию" not in session.scrollback()


def test_spinner_shows_summarization_during_compression(app, stub):
    """Во время запроса суммаризатора подпись индикатора — «Суммаризация...»."""
    stub.sequence(
        answer("Первый ответ."),
        answer("Второй ответ."),
        answer("Третий ответ."),
        Reply(content="РЕЗЮМЕ ДИАЛОГА.", delay=0.6),
        answer("Четвёртый ответ."),
    )
    with app() as session:
        set_compress_after(session, "5")
        session.ask("Вопрос один", "Первый ответ.")
        harness.wait_for_answers(session, 1)
        session.ask("Вопрос два", "Второй ответ.")
        harness.wait_for_answers(session, 2)
        session.ask("Вопрос три", "Третий ответ.")
        harness.wait_for_answers(session, 3)

        session.wait_for_prompt()
        session.send_line("Вопрос четыре")
        deadline = time.monotonic() + 3
        seen = False
        while time.monotonic() < deadline:
            if "Суммаризация" in session.screen_text():
                seen = True
                break
            time.sleep(0.02)
        assert seen, session.screen_text()
        session.wait_for("Контекст сжат: 4 сообщений (2 обменов) → резюме")
        harness.wait_for_answers(session, 4)
        session.wait_for_prompt()
