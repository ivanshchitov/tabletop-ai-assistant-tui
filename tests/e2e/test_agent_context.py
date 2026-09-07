"""Контекст сессии в запросах: агент пересылает стек сообщений, /clear и /logictask его не путают."""

import pytest

from . import harness
from .stub_api import answer

pytestmark = [pytest.mark.e2e, pytest.mark.pty]


def _roles(payload):
    return [m["role"] for m in payload["messages"]]


def test_second_question_carries_first_exchange(app, stub):
    stub.always(answer("Первый ответ stub-модели."))
    with app() as session:
        session.ask("Первый вопрос", "Первый ответ stub-модели.")
        session.send_line("Второй вопрос")
        session.wait_for("Диалогов за сессию: 2")

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
        session.wait_for("Диалогов за сессию: 2")

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
        session.wait_for("Диалогов за сессию: 2")

    # вызов прогона (запрос 2) — ровно два сообщения, вне стека
    assert _roles(stub.payload_at(1)) == ["system", "user"]
    # обычный вопрос после прогона — тот же состав контекста, что и до него
    payload = stub.payload_at(2)
    assert _roles(payload) == ["system", "user", "assistant", "user"]
    assert "Обычный вопрос" in payload["messages"][1]["content"]


def test_replayed_history_is_not_sent_to_the_model(app, stub, history_file):
    stub.always(answer("Ответ первой сессии."))
    with app() as session:
        session.ask("Вопрос в первой сессии", "Ответ первой сессии.")

    stub.always(answer("Ответ второй сессии."))
    with app() as session:
        session.wait_for("Вопрос в первой сессии")  # реплей виден на экране
        session.ask("Вопрос во второй сессии", "Ответ второй сессии.")

    # реплей остался показом: контекст второй сессии пуст
    assert _roles(stub.payload_at(1)) == ["system", "user"]
    assert "Вопрос в первой сессии" not in "".join(
        m["content"] for m in stub.payload_at(1)["messages"]
    )
