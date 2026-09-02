"""Прогон приложения на записанных ответах настоящей модели.

Два режима:
* обычный — stub отдаёт содержимое кассеты, тесты проверяют, что приложение корректно
  показывает и сохраняет реальный текст модели (длинный markdown, JSON-блоки, фразу отказа);
* `-m network --record-cassettes` — кассеты перезаписываются обращением к живому API.

Если кассеты ещё не записаны, тесты пропускаются: репозиторий остаётся рабочим без ключа.
"""

import json

import pytest

from core import config, prompts
from core.answer_settings import AnswerFormat, AnswerSettings
from ui.settings_screen import FORMAT_VALUES

from . import cassettes
from .harness import KEY_ESC, KEY_RIGHT, plain_tail
from .stub_api import answer

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

FORMAT_KEYPRESSES = {
    # Сколько раз нажать «вправо» из формата по умолчанию, чтобы попасть в нужный.
    fmt: (FORMAT_VALUES.index(fmt) - FORMAT_VALUES.index(AnswerSettings().format)) % len(FORMAT_VALUES)
    for fmt in FORMAT_VALUES
}


def _cassette(fmt: AnswerFormat) -> dict:
    data = cassettes.load(fmt.value)
    if not data:
        pytest.skip(
            f"Кассета {fmt.value} не записана — "
            "выполните pytest tests/e2e/test_recorded_answers.py -m network --record-cassettes"
        )
    return data


def _select_format(session, fmt: AnswerFormat) -> None:
    presses = FORMAT_KEYPRESSES[fmt]
    if not presses:
        return
    session.wait_for_prompt()
    session.send_line("/settings")
    session.wait_on_screen("Лимит вариантов в списке")
    for _ in range(presses):
        session.send_keys(KEY_RIGHT)
    session.send_keys(KEY_ESC)
    session.wait_until_gone("Лимит вариантов в списке")


# --- проигрывание кассет ---------------------------------------------------------------------


@pytest.mark.parametrize("question_key", sorted(cassettes.RECORDED_QUESTIONS))
def test_real_free_format_answers_are_displayed(app, stub, question_key):
    """Настоящий ответ модели должен доезжать до экрана целиком, без обрывов разметки."""
    recorded = _cassette(AnswerFormat.FREE)
    text = recorded[question_key]
    stub.always(answer(text))

    tail = plain_tail(text)
    with app() as session:
        session.ask(cassettes.RECORDED_QUESTIONS[question_key], tail, timeout=20)


def test_real_off_topic_answer_is_the_refusal_phrase(app, stub):
    """Ключевое продуктовое требование: посторонний вопрос получает точную фразу отказа."""
    recorded = _cassette(AnswerFormat.FREE)
    assert cassettes.REFUSAL_PHRASE in " ".join(recorded["off_topic"].split()), (
        "Записанный ответ на посторонний вопрос не содержит фразу отказа — "
        "либо промпт перестал работать, либо кассета устарела."
    )

    stub.always(answer(recorded["off_topic"]))
    with app() as session:
        session.ask("Какая погода завтра в Москве?", "работаю только с вопросами по настольным играм")


def test_real_json_answer_passes_client_side_validation(app, stub):
    """Записанный ответ в JSON-формате не должен вызывать предупреждение о невалидном JSON."""
    recorded = _cassette(AnswerFormat.JSON)
    stub.always(answer(recorded["description"]))

    with app() as session:
        _select_format(session, AnswerFormat.JSON)
        session.wait_for("Формат: JSON")
        session.send_line(cassettes.RECORDED_QUESTIONS["description"])
        session.wait_for("name_ru", timeout=20)
        session.read_for(0.5)
        assert not session.contains("Модель не вернула валидный JSON")


def test_real_answer_is_saved_to_history(app, stub, history_file):
    recorded = _cassette(AnswerFormat.FREE)
    text = recorded["rules"]
    stub.always(answer(text))

    with app() as session:
        session.ask(cassettes.RECORDED_QUESTIONS["rules"], plain_tail(text), timeout=20)
        session.send_line("/exit")
        session.wait_exit()

    saved = json.loads(history_file.read_text(encoding="utf-8"))
    assert saved[0]["answer"] == text


# --- запись кассет ------------------------------------------------------------------------------


@pytest.mark.network
@pytest.mark.parametrize("fmt", list(AnswerFormat), ids=lambda f: f.value)
def test_record_cassettes(request, fmt):
    """Обращается к живому OpenCode Zen и перезаписывает кассету выбранного формата."""
    if not request.config.getoption("--record-cassettes"):
        pytest.skip("запись кассет включается флагом --record-cassettes")

    from core.api_client import DeepseekAPIClient

    api_key = config.get_api_key()
    assert api_key, "Нужен настоящий OPENCODE_API_KEY в окружении или .env"

    client = DeepseekAPIClient(api_key)
    settings = AnswerSettings().with_format(fmt)
    recorded = {}
    for key, question in cassettes.RECORDED_QUESTIONS.items():
        recorded[key] = client.ask(
            prompts.build_system_message(fmt),
            prompts.build_user_prompt(question, settings),
            max_tokens=config.max_tokens_for_words(settings.max_words),
        )

    cassettes.save(fmt.value, recorded)
    assert cassettes.cassette_path(fmt.value).exists()
