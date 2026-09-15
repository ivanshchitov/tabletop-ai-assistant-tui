"""Профиль пользователя в живом приложении: диалог настройки, запрос, очистка и перезапуск."""

import json

import pytest

from . import harness

from .stub_api import answer

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

RESULT_QUESTION = "Подбери, во что нам поиграть вечером."

NOVICE_ANSWERS = [
    "новичок",
    "коротко и просто, без терминов",
    "только игры до часа, без таймеров, компания из четырёх",
    "новичок",
    "простые семейные, без конфликтов",
]
EXPERT_ANSWERS = [
    "эксперт",
    "развёрнуто и по делу",
    "только тяжёлые стратегии, партия от двух часов",
    "прожжёный настольщик, 300+ партий",
    "евро, построение движка",
]


def _saved(profile_file) -> dict:
    return json.loads(profile_file.read_text(encoding="utf-8"))


def setup_profile(session, answers) -> None:
    """Проходит диалог настройки: ждёт отрисованный вопрос и отвечает на него строкой.

    Ждём именно вопрос приложения, а не эхо собственного ввода: эхо появляется раньше, чем
    приложение готово принять следующий ответ, и отправка «по эху» съедала бы ответ.
    """
    session.wait_for("Вопрос 1 из 5")
    for index, reply in enumerate(answers, start=1):
        session.send_line(reply)
        if index < len(answers):
            session.wait_for(f"Вопрос {index + 1} из 5")
    session.wait_for("сохранён и активен")


def _profile_message(payload, index: int = 1) -> str:
    return [m["content"] for m in payload["messages"] if m["role"] == "system"][index]


def test_setup_writes_the_profile_file_and_reports_it(app, stub, profile_file):
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/profile setup")
        setup_profile(session, NOVICE_ANSWERS)
        session.send_line("/profile")
        session.wait_for("Активный профиль: новичок")

    saved = _saved(profile_file)
    assert saved["active"] == "новичок"
    assert saved["profiles"]["новичок"]["style"] == "коротко и просто, без терминов"
    assert saved["profiles"]["новичок"]["genres"] == "простые семейные, без конфликтов"
    assert "Ограничения: только игры до часа, без таймеров, компания из четырёх" in session.scrollback()
    # Активный профиль виден в статус-баре после каждого шага — как модель и стратегия.
    assert "Профиль: новичок  |  Формат:" in session.scrollback()


def test_question_after_setup_carries_the_profile_message(app, stub):
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/profile setup")
        setup_profile(session, NOVICE_ANSWERS)
        session.ask(RESULT_QUESTION, "Ответ stub-модели.")

    payload = stub.last_payload()
    assert "Профиль пользователя «новичок»" in _profile_message(payload)
    assert "Стиль: коротко и просто, без терминов" in _profile_message(payload)
    assert payload["messages"][-1]["role"] == "user"


def test_two_profiles_give_different_requests_for_the_same_question(app, stub):
    """Ради этого профиль и нужен: один вопрос, разные запросы — значит, разные ответы."""
    stub.sequence(answer("Ответ новичку."), answer("Ответ эксперту."))
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/profile setup")
        setup_profile(session, NOVICE_ANSWERS)
        session.ask(RESULT_QUESTION, "Ответ новичку.")

        session.send_line("/clear")
        session.wait_for("профиль пользователя сохранены")
        session.send_line("/profile setup")
        setup_profile(session, EXPERT_ANSWERS)
        session.ask(RESULT_QUESTION, "Ответ эксперту.")

    novice = _profile_message(stub.payload_at(0))
    expert = _profile_message(stub.payload_at(1))
    assert "Профиль пользователя «новичок»" in novice
    assert "Профиль пользователя «эксперт»" in expert
    assert "Стиль: развёрнуто и по делу" in expert
    assert "Стиль: коротко и просто" not in expert


def test_clear_keeps_the_profile_and_the_next_question(app, stub):
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/profile setup")
        setup_profile(session, NOVICE_ANSWERS)
        session.ask(RESULT_QUESTION, "Ответ stub-модели.")
        session.send_line("/clear")
        session.wait_for("профиль пользователя сохранены")
        session.send_line("/profile")
        session.wait_for("Активный профиль: новичок")
        session.ask("А теперь что посоветуешь?", "Ответ stub-модели.")

    payload = stub.last_payload()
    assert "Профиль пользователя «новичок»" in _profile_message(payload)
    # Диалога в запросе нет: ходов assistant не осталось, а профиль всё равно уходит.
    assert [m["role"] for m in payload["messages"]].count("assistant") == 0
    assert payload["messages"][-1]["role"] == "user"


def test_profile_survives_restart(app, stub, profile_file):
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/profile setup")
        setup_profile(session, NOVICE_ANSWERS)

    with app() as session:
        session.wait_for_prompt()
        session.send_line("/profile")
        session.wait_for("Активный профиль: новичок")
        session.ask(RESULT_QUESTION, "Ответ stub-модели.")

    assert "Профили файла: новичок" in session.scrollback()
    assert "Профиль пользователя «новичок»" in _profile_message(stub.last_payload())


def test_interview_can_be_cancelled_and_the_session_goes_on(app, stub, profile_file):
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/profile setup")
        session.wait_for("Вопрос 1 из 5")
        session.send_line("новичок")
        session.wait_for("Вопрос 2 из 5")
        session.send_key(harness.CTRL_C)
        session.wait_for("Настройка профиля отменена")
        session.send_line("/profile")
        session.wait_for("Активного профиля нет")

    saved = _saved(profile_file) if profile_file.exists() else {"profiles": {}}
    assert saved["profiles"] == {}


def test_real_profile_file_of_the_repository_is_not_touched(app, stub, profile_file):
    """Без переопределения пути прогон читал бы и писал реальный profile.json репозитория."""
    repo_profile = harness.REPO_ROOT / "profile.json"
    before = repo_profile.stat().st_mtime_ns if repo_profile.exists() else None
    stub.always(answer("Ответ stub-модели."))

    with app() as session:
        session.wait_for_prompt()
        session.send_line("/profile setup")
        setup_profile(session, NOVICE_ANSWERS)

    after = repo_profile.stat().st_mtime_ns if repo_profile.exists() else None
    assert before == after
    assert profile_file.exists()
