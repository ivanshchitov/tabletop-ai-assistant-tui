"""Слои памяти в живом приложении: маршрут записи, отчёт /memory, /clear и перезапуск."""

import json

import pytest

from . import harness

from .stub_api import answer

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

RESULT_QUESTION = "Я опытный игрок, у меня больше 300 партий. Собери партию на вечер."


def _saved(memory_file) -> dict:
    return json.loads(memory_file.read_text(encoding="utf-8"))


def test_question_with_turns_lands_in_the_layers_with_a_journal_line(app, stub):
    """Реплика с оборотами расписывается по слоям, и журнал говорит, куда именно."""
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.ask(RESULT_QUESTION, "Ответ stub-модели.")
        session.wait_for("Память: долговременная (профиль: опыт)")

    payload = stub.last_payload()
    memory = [m["content"] for m in payload["messages"] if m["role"] == "system"][1]
    assert "Рабочая память" in memory
    assert "Долговременная память" in memory
    assert "300 партий" in memory
    assert payload["messages"][-1]["role"] == "user"


def test_clue_less_question_leaves_the_layers_alone(app, stub, memory_file):
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.ask("Какие правила у Каркассона?", "Ответ stub-модели.")
        session.send_line("/memory")
        session.wait_for("Память агента")

    assert not memory_file.exists() or _saved(memory_file)["entries"] == {}


def test_memory_report_shows_both_layers_and_the_stores(app, stub, memory_file, history_file):
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.ask(RESULT_QUESTION, "Ответ stub-модели.")
        session.send_line("/memory")
        session.wait_for("Правила маршрутизации")

    assert "Краткосрочная: 1 обмен диалога" in session.scrollback()
    assert "Долговременная: 1 запись" in session.scrollback()
    assert memory_file.name in session.scrollback()
    assert history_file.name in session.scrollback()
    assert _saved(memory_file)["entries"]["опыт"]["category"] == "профиль"


def test_clear_keeps_the_long_term_layer_but_wipes_the_working_one(app, stub, memory_file, history_file):
    """Команда /clear отвечает за диалог: сведения о пользователе переживают её."""
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.ask(RESULT_QUESTION, "Ответ stub-модели.")
        session.send_line("/clear")
        session.wait_for("долговременная память и профиль пользователя сохранены")
        session.send_line("/memory")
        session.wait_for("Краткосрочная: 0 обменов диалога")

    assert "опыт" in session.scrollback()  # запись долговременного слоя на месте
    assert _saved(memory_file)["entries"]["опыт"]["value"]
    assert _saved(history_file)["working"] == {}
    assert _saved(history_file)["dialogues"] == []


def test_restart_restores_the_layers_from_their_own_files(app, stub, memory_file):
    """После перезапуска долговременная память и цель задачи возвращаются в запрос."""
    stub.always(answer("Ответ первой сессии."))
    with app() as session:
        session.ask(RESULT_QUESTION, "Ответ первой сессии.")
        session.send_line("/exit")
        session.wait_exit()

    stub.always(answer("Ответ второй сессии."))
    with app() as session:
        session.wait_for("Ответ первой сессии")
        session.ask("Что посоветуешь на вечер?", "Ответ второй сессии.")

    memory = [m["content"] for m in stub.last_payload()["messages"] if m["role"] == "system"][1]
    assert "300 партий" in memory  # долговременный слой из своего файла
    assert "партию на вечер" in memory  # рабочая память из конверта истории
    assert _saved(memory_file)["entries"]["опыт"]["category"] == "профиль"


def test_manual_memory_commands_and_forget_all(app, stub, memory_file):
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/memory goal Подобрать игру на вечер")
        session.wait_for("Рабочая память: цель")
        session.send_line("/memory remember Я не люблю игры с таймером")
        session.wait_for("Долговременная память: профиль · предпочтения")
        session.send_line("/memory forget опыт")
        session.wait_for("Записи «опыт» нет ни в одном слое")
        session.send_line("/memory forget all")
        session.wait_for("Краткосрочная память — ход текущего диалога — не тронута")

    assert _saved(memory_file)["entries"] == {}


def test_memory_command_asks_no_model(app, stub):
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/memory")
        session.wait_for("Память агента")
        session.send_line("/exit")
        session.wait_exit()

    assert stub.call_count == 0


def test_commands_panel_lists_memory_command(app, stub):
    session = app()
    session.wait_for_prompt()
    session.send_line("/commands")
    session.wait_on_screen("/memory")
    session.send_key(harness.KEY_ESC, 1)
    session.wait_until_gone("/memory")
    session.send_line("/exit")
    session.wait_exit()
