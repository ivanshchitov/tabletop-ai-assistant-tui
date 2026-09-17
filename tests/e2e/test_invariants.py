"""Инварианты агента в живом приложении: сообщение в запросе, повтор, отклонение, отчёт, конвейер."""

import json

import pytest

from core.invariants import REFUSAL_PREFIX, invariants_message

from .stub_api import answer

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

QUESTION = "Что взять на вечер?"
PLAN_JSON = '{"items": ["Тема", "Ход игрока", "Подсчёт очков"]}'
OK_JSON = '{"ok": true, "issues": []}'


def _systems(payload) -> list:
    return [m["content"] for m in payload["messages"] if m["role"] == "system"]


def test_every_question_carries_the_invariants_message_after_the_profile(app, stub):
    stub.always(answer("Берите Каркассон."))
    with app() as session:
        session.ask(QUESTION, "Берите Каркассон.")
        session.ask("А ещё?", "Токены: 50+100=150")

    for index in range(stub.call_count):
        systems = _systems(stub.payload_at(index))
        # Профиля нет — инварианты сразу после system настроек.
        assert systems[1] == invariants_message()
        assert "1. Только физические компоненты" in systems[1]
        assert REFUSAL_PREFIX in systems[1]
    assert stub.last_payload()["messages"][-1]["role"] == "user"


def test_violation_is_retried_with_the_violations_and_the_clean_retry_is_shown(app, stub):
    stub.sequence(answer("Берите Монополию!"), answer("Берите Каркассон."))
    with app() as session:
        session.ask(QUESTION, "Берите Каркассон.")
        text = session.wait_for("Токены:")

    assert stub.call_count == 2
    assert "⛔ Инвариант 3 нарушен («монопол») — повторный запрос" in text
    assert "Ответ отклонён" not in text
    retry = stub.payload_at(1)["messages"]
    assert retry[-2] == {"role": "assistant", "content": "Берите Монополию!"}
    assert retry[-1]["role"] == "user"
    assert "инвариант 3" in retry[-1]["content"] and "«монопол»" in retry[-1]["content"]


def test_second_violation_rejects_the_answer_and_the_refusal_lands_in_history(app, stub, history_file):
    stub.sequence(answer("Берите Монополию!"), answer("Ну возьмите монополию."))
    with app() as session:
        session.ask(QUESTION, f"{REFUSAL_PREFIX}: ответ нарушает инвариант 3")
        text = session.wait_for("Токены:")

    assert stub.call_count == 2
    assert "⛔ Ответ отклонён: инвариант 3" in text
    assert "Ну возьмите" not in text
    saved = json.loads(history_file.read_text(encoding="utf-8"))
    (record,) = saved["dialogues"]
    assert record["question"] == QUESTION
    assert record["answer"].startswith(REFUSAL_PREFIX)
    assert "«монопол»" in record["answer"]


def test_model_refusal_is_shown_as_is_without_a_retry(app, stub):
    refusal = f"{REFUSAL_PREFIX}: инвариант 1 запрещает игры с приложением на смартфоне."
    stub.always(answer(refusal))
    with app() as session:
        session.ask("Хочу игру с приложением", "запрещает игры с приложением")
        text = session.wait_for("Токены:")

    assert stub.call_count == 1
    assert "⛔" not in text


def test_invariants_report_needs_no_request(app, stub):
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/invariants")
        text = session.wait_for("Отказ модели по инварианту")

    assert stub.call_count == 0
    assert "1. Только физические компоненты" in text
    assert "проверка кодом по словам: приложени" in text
    assert "2. Партия не дольше двух часов" in text
    assert "только модель" in text
    assert "в каждый запрос конвейера /task" in text


def test_clear_keeps_the_invariants_in_the_next_request(app, stub):
    stub.always(answer("Берите Каркассон."))
    with app() as session:
        session.ask(QUESTION, "Берите Каркассон.")
        session.send_line("/clear")
        session.wait_for("История диалога очищена")
        session.ask("А теперь?", "Токены: 50+100=150")

    payload = stub.last_payload()
    assert _systems(payload)[1] == invariants_message()
    assert [m["role"] for m in payload["messages"]].count("assistant") == 0


def test_pipeline_requests_carry_the_invariants_message_second(app, stub):
    stub.sequence(
        answer(PLAN_JSON), answer("Раздел 1"), answer("Раздел 2"), answer("Раздел 3"), answer(OK_JSON)
    )
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/task add Разработать игру про улиток")
        session.wait_for("Задача в очереди")
        session.send_line("/task run")
        session.wait_for("Правки к плану")
        session.send_line("")
        session.wait_for("Итог: ")

    assert stub.call_count == 5
    for index in range(stub.call_count):
        messages = stub.payload_at(index)["messages"]
        assert [m["role"] for m in messages[:2]] == ["system", "system"]
        assert messages[1]["content"] == invariants_message()
        assert messages[0]["content"] != invariants_message()
