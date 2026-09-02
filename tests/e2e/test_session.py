"""Сценарии живой работы с приложением: запуск, вопросы, история, выход.

Каждый тест поднимает настоящий процесс в псевдотерминале и общается с ним так же, как
человек. Проверяется и то, что видно на экране, и то, что при этом реально ушло в API.
"""

import json

import pytest

from .harness import CTRL_C, CTRL_D, KEY_TAB
from .stub_api import answer, failure, hang, malformed

pytestmark = [pytest.mark.e2e, pytest.mark.pty]


# --- старт ---------------------------------------------------------------------------------


def test_greets_on_first_launch(app):
    with app() as session:
        session.wait_for("TABLETOP AI ASSISTANT")
        session.wait_for("Tabletop AI Assistant запущен")
        session.wait_for_prompt()
        assert session.contains("Статус: Готов")


def test_asks_for_key_when_environment_has_none(app, stub):
    """Первый запуск без ключа: приложение просит его ввести и продолжает работать."""
    stub.always(answer("Ответ после ввода ключа."))
    with app(api_key="") as session:
        session.wait_for("API-ключ не найден")
        session.send_line("sk-manual-key-12345")
        session.wait_for_prompt()
        session.send_line("Вопрос")
        session.wait_for("Ответ после ввода ключа")

    assert stub.requests[0]["headers"]["Authorization"] == "Bearer sk-manual-key-12345"


def test_empty_key_input_is_asked_again(app, stub):
    """Пустой ввод не принимается за ключ — приложение спрашивает снова."""
    stub.always(answer("Ответ."))
    with app(api_key="") as session:
        session.wait_for("API-ключ не найден")
        session.send_line("")
        session.send_line("   ")
        session.send_line("sk-manual-key-12345")
        session.wait_for_prompt()

    assert not session.contains("Traceback")


def test_non_ascii_key_is_rejected_at_input(app, stub):
    """Ключ, набранный в русской раскладке, отсекается сразу — без падения с traceback."""
    with app(api_key="") as session:
        session.wait_for("API-ключ не найден")
        session.send_line("sk-введён-вручную")
        session.wait_for("проверьте раскладку клавиатуры")

        session.send_line("sk-manual-key-12345")
        session.wait_for_prompt()
        session.send_line("Вопрос")
        session.wait_for("Ответ stub-сервера")
        assert "Traceback" not in session.scrollback()

    assert stub.requests[0]["headers"]["Authorization"] == "Bearer sk-manual-key-12345"


def test_non_ascii_key_from_environment_is_reported_at_startup(app, stub):
    """Непригодный ключ из .env тоже отсекается на старте, а не после первого вопроса."""
    with app(api_key="sk-ключ-из-окружения") as session:
        session.wait_for("проверьте раскладку клавиатуры")
        session.send_line("sk-manual-key-12345")
        session.wait_for_prompt()
        session.send_line("Вопрос")
        session.wait_for("Ответ stub-сервера")
        assert "Traceback" not in session.scrollback()

    assert stub.call_count == 1


# --- обычный диалог ------------------------------------------------------------------------


def test_answers_a_question(app, stub):
    stub.always(answer("**Каркассон** — игра о выкладывании тайлов. 🎲"))
    with app() as session:
        session.ask("Расскажи про Каркассон", "выкладывании тайлов")
        assert session.contains("Вы: Расскажи про Каркассон")
        assert session.contains("Tabletop AI Assistant:")

    assert stub.call_count == 1
    assert "Расскажи про Каркассон" in stub.user_messages()[0]


def test_three_questions_keep_the_log_and_count_the_session(app, stub):
    """Лог append-only: прошлые обмены остаются выше, счётчик за сессию растёт."""
    stub.sequence(
        answer("Первый ответ про правила."),
        answer("Второй ответ про стратегию."),
        answer("Третий ответ про рекомендации."),
    )
    with app() as session:
        session.ask("Вопрос про правила", "Первый ответ")
        session.wait_for("Диалогов за сессию: 1")
        session.ask("Вопрос про стратегию", "Второй ответ")
        session.wait_for("Диалогов за сессию: 2")
        session.ask("Вопрос про рекомендации", "Третий ответ")
        session.wait_for("Диалогов за сессию: 3")

        log = session.scrollback()

    for fragment in ("Первый ответ", "Второй ответ", "Третий ответ"):
        assert fragment in log
    assert stub.call_count == 3


def test_markdown_answer_is_rendered_completely(app, stub):
    """Анимация печати должна доходить до конца — последний абзац не теряется."""
    stub.always(
        answer(
            "# Правила\n\n"
            "1. Возьмите тайл\n"
            "2. Положите его рядом\n\n"
            "**Важно:** мипла ставят только на свежий тайл.\n\n"
            "Итог: следите за очками. 🏆"
        )
    )
    with app() as session:
        session.ask("Как ходить?", "Итог: следите за очками")
        log = session.scrollback()

    assert "Правила" in log
    assert "Возьмите тайл" in log
    assert "мипла ставят только на свежий тайл" in log


def test_empty_input_does_not_reach_the_api(app, stub):
    with app() as session:
        session.wait_for_prompt()
        session.send_line("")
        session.send_line("   ")
        session.read_for(0.4)
        session.send_line("/exit")
        session.wait_exit()

    assert stub.call_count == 0


def test_long_question_is_truncated(app, stub):
    with app() as session:
        session.wait_for_prompt()
        session.send_line("я" * 2500)
        session.wait_for("Запрос слишком длинный")
        session.wait_for("Ответ stub-сервера")

    # Считать вхождения по всему промпту нельзя: буква встречается и в шаблонных
    # фразах инструкции, поэтому проверяем длину самой цепочки.
    sent = stub.user_messages()[0]
    assert "я" * 2000 in sent
    assert "я" * 2001 not in sent


# --- история между запусками -----------------------------------------------------------------


def test_history_is_replayed_after_restart(app, stub, history_file):
    stub.always(answer("Мипл ставится на завершённый элемент."))
    with app() as first:
        first.ask("Куда ставить мипла?", "завершённый элемент")
        first.send_line("/exit")
        first.wait_exit()

    assert json.loads(history_file.read_text(encoding="utf-8"))[0]["question"] == "Куда ставить мипла?"

    with app() as second:
        second.wait_for("Куда ставить мипла?")
        second.wait_for("завершённый элемент")
        assert not second.contains("Tabletop AI Assistant запущен")


def test_clear_wipes_history_for_the_next_launch(app, stub, history_file):
    stub.always(answer("Ответ, который потом сотрут."))
    with app() as first:
        first.ask("Вопрос", "который потом сотрут")
        first.send_line("/clear")
        first.wait_for("История диалога очищена")
        first.send_line("/exit")
        first.wait_exit()

    assert json.loads(history_file.read_text(encoding="utf-8")) == []

    with app() as second:
        second.wait_for("Tabletop AI Assistant запущен")
        assert not second.contains("который потом сотрут")


# --- ошибки ---------------------------------------------------------------------------------


def test_server_error_is_survivable(app, stub):
    """Ошибка не роняет сессию: следующий вопрос проходит, в историю попал только успешный."""
    stub.sequence(failure(500), answer("А вот теперь всё хорошо."))
    with app() as session:
        session.ask("Первый вопрос", "Ошибка Deepseek API")
        session.wait_for("Попробуйте повторить запрос")
        session.ask("Второй вопрос", "А вот теперь всё хорошо")
        session.wait_for("Диалогов за сессию: 1")


def test_bad_key_is_reported(app, stub):
    stub.always(failure(401))
    with app() as session:
        session.ask("Вопрос", "Неверный API-ключ")


def test_malformed_response_is_reported(app, stub):
    stub.always(malformed())
    with app() as session:
        session.ask("Вопрос", "Некорректный ответ от Deepseek API")


def test_timeout_is_retried_transparently(app, stub):
    """Пользователь видит один ответ, хотя под капотом было три попытки."""
    stub.sequence(hang(), hang(), answer("Успели с третьей попытки."))
    with app() as session:
        session.ask("Вопрос", "Успели с третьей попытки", timeout=20)

    assert stub.call_count == 3


def test_failed_exchange_is_not_saved(app, stub, history_file):
    stub.always(failure(500))
    with app() as session:
        session.ask("Вопрос без ответа", "Ошибка Deepseek API")
        session.send_line("/exit")
        session.wait_exit()

    assert json.loads(history_file.read_text(encoding="utf-8")) == []


# --- выход ------------------------------------------------------------------------------------


def test_exit_command(app):
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/exit")
        session.wait_for("До встречи!")
        assert session.wait_exit() == 0


def test_ctrl_d_exits_cleanly(app):
    with app() as session:
        session.wait_for_prompt()
        session.send_key(CTRL_D)
        session.wait_for("До встречи!")
        assert session.wait_exit() == 0


def test_ctrl_c_during_the_answer_exits_without_traceback(app, stub):
    """Ctrl+C во время ожидания ответа — штатное завершение, а не необработанное исключение."""
    stub.always(hang(seconds=10))
    with app(extra_env={"TABLETOP_REQUEST_TIMEOUT": "30"}) as session:
        session.wait_for_prompt()
        session.send_line("Долгий вопрос")
        session.wait_for("Отправка")
        session.send_key(CTRL_C)
        session.wait_for("До встречи!")
        assert session.wait_exit() == 0
        assert "Traceback" not in session.scrollback()


def test_ctrl_c_at_the_prompt_exits_cleanly(app):
    with app() as session:
        session.wait_for_prompt()
        session.send_key(CTRL_C)
        session.wait_for("До встречи!")
        assert session.wait_exit() == 0
        assert "Traceback" not in session.scrollback()


def test_ctrl_c_while_waiting_for_the_key_exits_cleanly(app):
    with app(api_key="") as session:
        session.wait_for("API-ключ не найден")
        session.send_key(CTRL_C)
        session.wait_for("До встречи!")
        assert session.wait_exit() == 0
        assert "Traceback" not in session.scrollback()


# --- автодополнение ------------------------------------------------------------------------------


def test_tab_completes_a_command(app):
    """readline активен только на настоящем терминале — проверить это можно лишь здесь."""
    with app() as session:
        session.wait_for_prompt()
        session.send_key(b"/set")
        session.send_key(KEY_TAB)
        session.read_for(0.4)
        assert session.contains("/settings")
        session.send_key(CTRL_C)
        session.wait_exit()
