"""Главный цикл приложения с подставленными консолью, историей и клиентом."""

import contextlib
import json
from pathlib import Path
from typing import List, Optional

import pytest

from core import config
from core.answer_settings import AnswerFormat, AnswerSettings
from core.api_client import DeepseekAPIError
from core.history_manager import HistoryManager
from ui import keyboard, settings_screen, tui_app
from ui.tui_app import TabletopAITUI


class FakeClient:
    """Подставной клиент: отдаёт заготовленные ответы и запоминает, что у него спросили.

    temperature=None означает «вызывающий не передал температуру» — так отличают вызов,
    полагающийся на дефолт клиента (/logictask), от явной передачи значения настройки.
    """

    def __init__(self, answers=None, error: Optional[Exception] = None) -> None:
        self.answers = list(answers or ["Ответ по умолчанию"])
        self.error = error
        self.calls: List[dict] = []

    def ask(
        self,
        system_message: str,
        user_message: str,
        max_tokens: int = 0,
        temperature: Optional[float] = None,
    ) -> str:
        self.calls.append(
            {
                "system": system_message,
                "user": user_message,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self.error is not None:
            raise self.error
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


@contextlib.contextmanager
def _noop_context():
    """Замена keyboard.raw_mode() там, где настоящего терминала нет."""
    yield


@pytest.fixture(autouse=True)
def instant_typing(monkeypatch):
    """Убирает анимацию печати — она не влияет на итоговый текст, но тормозит прогон."""
    monkeypatch.setattr(tui_app, "TYPING_DELAY", 0)


@pytest.fixture
def make_app(recording_console, history):
    def factory(inputs: List[str], client: Optional[FakeClient] = None) -> TabletopAITUI:
        client = client or FakeClient()
        app = TabletopAITUI(console=recording_console.console, history=history, client=client)
        app._inputs = iter(inputs)  # используется подменённым input() ниже
        return app

    return factory


@pytest.fixture(autouse=True)
def scripted_input(monkeypatch):
    """Подменяет input() на чтение из заранее заданного списка строк.

    Исчерпание списка означает Ctrl+D — так же, как в реальном терминале.
    """

    def fake_input(prompt: str = "") -> str:
        app = _current_app[0]
        try:
            return next(app._inputs)
        except StopIteration:
            raise EOFError

    _current_app: List[TabletopAITUI] = [None]

    original_run = TabletopAITUI.run

    def run(self):
        _current_app[0] = self
        return original_run(self)

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(TabletopAITUI, "run", run)


# --- запуск и завершение ------------------------------------------------------------------


def test_welcome_is_shown_on_empty_history(make_app, recording_console):
    make_app(["/exit"]).run()
    assert recording_console.contains("Tabletop AI Assistant запущен")


def test_existing_history_is_replayed_instead_of_welcome(make_app, recording_console, history):
    history.add("Правила Каркассона?", "Ставьте миплов.")
    make_app(["/exit"]).run()

    assert recording_console.contains("Правила Каркассона?")
    assert recording_console.contains("Ставьте миплов.")
    assert not recording_console.contains("Tabletop AI Assistant запущен")


def test_exit_command_says_goodbye(make_app, recording_console):
    make_app(["/exit"]).run()
    assert recording_console.contains(tui_app.GOODBYE_MESSAGE)


def test_end_of_input_exits_like_ctrl_d(make_app, recording_console):
    make_app([]).run()
    assert recording_console.contains(tui_app.GOODBYE_MESSAGE)


def test_empty_input_is_skipped(make_app):
    client = FakeClient()
    make_app(["", "   ", "/exit"], client).run()
    assert client.calls == []


# --- получение API-ключа --------------------------------------------------------------------


def monkeypatch_stdin_lines(monkeypatch, lines):
    """Ручной ввод ключа читается из sys.stdin.readline, а не из input() — см. _read_manual_key."""
    reader = iter(lines)

    class FakeStdin:
        def readline(self):
            try:
                return next(reader) + "\n"
            except StopIteration:
                raise EOFError  # исчерпание ввода = Ctrl+D

    monkeypatch.setattr("sys.stdin", FakeStdin())


def test_key_from_environment_is_used_without_asking(make_app, recording_console, monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-from-env")
    app = TabletopAITUI(console=recording_console.console, history=HistoryManager(path=Path("/dev/null")))
    assert app._ensure_api_key() == "sk-from-env"
    assert not recording_console.contains("API-ключ не найден")


def test_missing_key_is_requested(make_app, recording_console, monkeypatch, history):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    app = TabletopAITUI(console=recording_console.console, history=history)
    monkeypatch_stdin_lines(monkeypatch, ["", "   ", "sk-typed-by-hand"])

    assert app._ensure_api_key() == "sk-typed-by-hand"
    assert recording_console.contains("API-ключ не найден")


def test_non_ascii_key_is_rejected_and_asked_again(recording_console, monkeypatch, history):
    """Ключ в русской раскладке отсекается при вводе — иначе он падал бы UnicodeEncodeError."""
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    app = TabletopAITUI(console=recording_console.console, history=history)
    monkeypatch_stdin_lines(monkeypatch, ["sk-введён-вручную", "sk-good-key"])

    assert app._ensure_api_key() == "sk-good-key"
    assert recording_console.contains("проверьте раскладку клавиатуры")


def test_non_ascii_key_from_environment_is_rejected(recording_console, monkeypatch, history):
    """Непригодный ключ из .env отсекается на старте, а не после первого вопроса."""
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-ключ-из-окружения")
    app = TabletopAITUI(console=recording_console.console, history=history)
    monkeypatch_stdin_lines(monkeypatch, ["sk-good-key"])

    assert app._ensure_api_key() == "sk-good-key"
    assert recording_console.contains("проверьте раскладку клавиатуры")
    assert not recording_console.contains("API-ключ не найден")


# --- вопрос и ответ -------------------------------------------------------------------------


def test_question_reaches_the_client_and_the_answer_is_printed(make_app, recording_console):
    client = FakeClient(["Мипл ставится на завершённый элемент."])
    make_app(["Куда ставить мипла?", "/exit"], client).run()

    assert len(client.calls) == 1
    assert "Куда ставить мипла?" in client.calls[0]["user"]
    assert recording_console.contains("Мипл ставится на завершённый элемент.")


def test_successful_exchange_is_persisted(make_app, history, history_path):
    make_app(["Вопрос про Splendor", "/exit"], FakeClient(["Ответ"])).run()

    assert history.dialogues == [{"question": "Вопрос про Splendor", "answer": "Ответ"}]
    assert json.loads(history_path.read_text(encoding="utf-8"))[0]["question"] == "Вопрос про Splendor"


def test_session_counter_grows_with_each_answer(make_app, recording_console):
    app = make_app(["Вопрос 1", "Вопрос 2", "Вопрос 3", "/exit"], FakeClient(["Ответ"]))
    app.run()
    assert app.session_count == 3
    assert recording_console.contains("Диалогов за сессию: 3")


def test_long_input_is_truncated_before_sending(make_app, recording_console):
    client = FakeClient()
    long_question = "и" * (config.MAX_INPUT_LENGTH + 500)
    make_app([long_question, "/exit"], client).run()

    assert recording_console.contains("Запрос слишком длинный")
    assert "и" * config.MAX_INPUT_LENGTH in client.calls[0]["user"]
    assert "и" * (config.MAX_INPUT_LENGTH + 1) not in client.calls[0]["user"]


def test_max_tokens_follows_word_setting(make_app):
    client = FakeClient()
    app = make_app(["Вопрос", "/exit"], client)
    app.settings = AnswerSettings().with_max_words(60)
    app.run()
    assert client.calls[0]["max_tokens"] == config.max_tokens_for_words(60)


def test_system_message_matches_active_format(make_app):
    from core import prompts

    client = FakeClient()
    app = make_app(["Расскажи про Catan", "/exit"], client)
    app.settings = AnswerSettings().with_format(AnswerFormat.JSON)
    app.run()
    assert client.calls[0]["system"] == prompts.build_system_message(AnswerFormat.JSON)


# --- ошибки API -----------------------------------------------------------------------------


def test_api_error_is_reported_and_not_persisted(make_app, recording_console, history):
    client = FakeClient(error=DeepseekAPIError("Неверный API-ключ Deepseek."))
    app = make_app(["Вопрос", "/exit"], client)
    app.run()

    assert recording_console.contains("Неверный API-ключ Deepseek.")
    assert recording_console.contains("Попробуйте повторить запрос.")
    assert history.dialogues == []
    assert app.session_count == 0


def test_app_survives_an_error_and_answers_the_next_question(make_app, recording_console, history):
    class FlakyClient(FakeClient):
        def ask(self, system_message, user_message, max_tokens=0, temperature=None):
            self.calls.append({"user": user_message})
            if len(self.calls) == 1:
                raise DeepseekAPIError("Ошибка соединения с Deepseek API.")
            return "Второй ответ"

    app = make_app(["Первый вопрос", "Второй вопрос", "/exit"], FlakyClient())
    app.run()

    assert recording_console.contains("Ошибка соединения")
    assert recording_console.contains("Второй ответ")
    assert [d["question"] for d in history.dialogues] == ["Второй вопрос"]


# --- команды -----------------------------------------------------------------------------------


def test_clear_empties_history_and_file(make_app, recording_console, history, history_path):
    history.add("Старый вопрос", "Старый ответ")
    make_app(["/clear", "/exit"], FakeClient()).run()

    assert history.dialogues == []
    assert json.loads(history_path.read_text(encoding="utf-8")) == []
    assert recording_console.contains("История диалога очищена.")


def test_unknown_slash_command_is_sent_as_a_question(make_app):
    client = FakeClient()
    make_app(["/помощь", "/exit"], client).run()
    assert "/помощь" in client.calls[0]["user"]


def test_status_bar_shows_current_settings(make_app, recording_console):
    app = make_app(["/exit"], FakeClient())
    app.settings = AnswerSettings(max_words=75, format=AnswerFormat.COMPACT, list_limit=4)
    app.run()

    assert recording_console.contains("Формат: компактный")
    assert recording_console.contains("Объём: 75 слов")
    assert recording_console.contains("Лимит списка: 4")
    assert recording_console.contains("Температура: 0.7")


def test_status_bar_reflects_changed_temperature(make_app, recording_console):
    app = make_app(["/exit"], FakeClient())
    app.settings = app.settings.with_temperature(1.2)
    app.run()
    assert recording_console.contains("Температура: 1.2")


def test_settings_panel_shows_temperature_row(recording_console):
    """Панель /settings содержит строку температуры с диапазоном и текущим значением.

    Живой экран рисуется в transient-Live (в буфер юнит-теста не попадает), поэтому
    рендерим панель напрямую — проверяется состав строк, а не поведение Live.
    """
    app = TabletopAITUI(console=recording_console.console, history=HistoryManager(), client=FakeClient())
    recording_console.console.print(app._render_settings_panel(settings_screen.initial_state(app.settings)))

    assert recording_console.contains(
        f"Температура ({config.MIN_TEMPERATURE}..{config.MAX_TEMPERATURE}): 0.7"
    )


def test_settings_screen_applies_temperature_change(
    make_app, recording_console, monkeypatch
):
    """Набор 1.2 на строке температуры меняет настройку и виден в статус-баре."""
    keys = iter(
        [keyboard.DOWN, keyboard.DOWN, keyboard.DOWN]
        + [keyboard.BACKSPACE] * 3
        + ["1", ".", "2", keyboard.ESC]
    )
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    app = make_app(["/settings", "/exit"], FakeClient())
    app.run()

    assert app.settings.temperature == 1.2
    assert recording_console.contains("Температура: 1.2")


# --- предупреждение о невалидном JSON -------------------------------------------------------


def test_invalid_json_answer_is_flagged_but_still_shown(make_app, recording_console):
    client = FakeClient(["Каркассон — отличная игра, но это не JSON."])
    app = make_app(["Расскажи про Каркассон", "/exit"], client)
    app.settings = AnswerSettings().with_format(AnswerFormat.JSON)
    app.run()

    assert recording_console.contains("Модель не вернула валидный JSON")
    assert recording_console.contains("это не JSON")


def test_valid_json_answer_is_not_flagged(make_app, recording_console):
    client = FakeClient(['```json\n{"name_ru": "Каркассон"}\n```'])
    app = make_app(["Расскажи про Каркассон", "/exit"], client)
    app.settings = AnswerSettings().with_format(AnswerFormat.JSON)
    app.run()

    assert not recording_console.contains("Модель не вернула валидный JSON")


def test_json_warning_only_applies_to_json_format(make_app, recording_console):
    """В свободном формате проза — норма, предупреждать не о чем."""
    client = FakeClient(["Обычный текстовый ответ."])
    app = make_app(["Вопрос", "/exit"], client)
    app.settings = AnswerSettings().with_format(AnswerFormat.FREE)
    app.run()

    assert not recording_console.contains("не вернула валидный JSON")


# --- экран настроек внутри цикла --------------------------------------------------------------


def test_settings_screen_applies_changes_and_updates_status_bar(
    make_app, recording_console, monkeypatch
):
    """Полный путь /settings: клавиши идут в приложение, новые значения видны в статус-баре."""
    keys = iter(
        [keyboard.RIGHT, keyboard.DOWN]
        + [keyboard.BACKSPACE] * 3
        + ["5", "0", keyboard.DOWN, keyboard.BACKSPACE, "6", keyboard.ESC]
    )
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    app = make_app(["/settings", "/exit"], FakeClient())
    app.run()

    values = settings_screen.FORMAT_VALUES
    expected_format = values[(values.index(AnswerSettings().format) + 1) % len(values)]
    assert app.settings.max_words == 50
    assert app.settings.list_limit == 6
    assert app.settings.format == expected_format
    assert recording_console.contains("Объём: 50 слов")
    assert recording_console.contains("Лимит списка: 6")


def test_settings_screen_reports_invalid_value_and_keeps_previous(
    make_app, recording_console, monkeypatch
):
    keys = iter([keyboard.DOWN] + [keyboard.BACKSPACE] * 3 + ["9", "9", "9", keyboard.ESC])
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    app = make_app(["/settings", "/exit"], FakeClient())
    app.run()

    assert app.settings.max_words == config.DEFAULT_MAX_WORDS
    assert recording_console.contains(f"{config.MIN_MAX_WORDS}..{config.MAX_MAX_WORDS}")


def test_settings_change_affects_the_next_request(make_app, monkeypatch):
    """Главное следствие /settings: изменившийся промпт и потолок токенов в следующем запросе."""
    keys = iter(
        [keyboard.DOWN] + [keyboard.BACKSPACE] * 3 + ["8", "0", keyboard.DOWN,
                                                      keyboard.BACKSPACE, "2", keyboard.ESC]
    )
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    client = FakeClient()
    make_app(["/settings", "Что посоветуешь вдвоём?", "/exit"], client).run()

    assert "не более 80 слов" in client.calls[0]["user"]
    assert "не более 2 вариантов" in client.calls[0]["user"]
    assert client.calls[0]["max_tokens"] == config.max_tokens_for_words(80)


def test_escape_immediately_leaves_settings_untouched(make_app, monkeypatch):
    monkeypatch.setattr(keyboard, "read_key", lambda: keyboard.ESC)
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    app = make_app(["/settings", "/exit"], FakeClient())
    before = app.settings
    app.run()
    assert app.settings == before


# --- команда /logictask: выбор стратегии и прогон -------------------------------------------


def test_logictask_opens_panel_and_runs_chosen_strategy(make_app, recording_console, monkeypatch):
    """↓ + Enter: выбрана стратегия 2 — ровно один запрос, ответ под её заголовком."""
    keys = iter([keyboard.DOWN, keyboard.ENTER, keyboard.ESC])
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    client = FakeClient(["Пошаговый ответ модели"])
    app = make_app(["/logictask", "/exit"], client)
    app.run()

    assert len(client.calls) == 1
    from core import logictask

    assert client.calls[0]["system"] == logictask.STEPWISE_SYSTEM_MESSAGE
    assert logictask.LOGIC_TASK in client.calls[0]["user"]
    assert recording_console.contains("Стратегия 2: Пошаговое решение")
    assert recording_console.contains("Пошаговый ответ модели")
    assert app.session_count == 0


def test_question_carries_temperature_setting(make_app):
    """Температура настройки сессии доходит до клиента с каждым вопросом."""
    client = FakeClient()
    app = make_app(["Вопрос", "/exit"], client)
    app.settings = AnswerSettings().with_temperature(1.2)
    app.run()
    assert client.calls[0]["temperature"] == 1.2


def test_logictask_ignores_temperature_setting(make_app, monkeypatch):
    """Прогоны /logictask не передают температуру настройки — клиент берёт дефолт."""
    keys = iter([keyboard.ENTER, keyboard.ESC])
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    client = FakeClient(["Ответ задачи"])
    app = make_app(["/logictask", "/exit"], client)
    app.settings = AnswerSettings().with_temperature(1.2)
    app.run()
    assert client.calls[0]["temperature"] is None


def test_logictask_panel_visible_before_choice(make_app, recording_console, monkeypatch):
    """Панель с четырьмя стратегиями и описанием задачи; до Enter/Esc запросов нет."""
    keys = iter([keyboard.ESC])
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    from core import logictask

    client = FakeClient()
    app = make_app(["/logictask", "/exit"], client)
    app.run()

    assert recording_console.contains("Выберите стратегию")
    for _, title in ((1, "Прямой ответ"), (2, "Пошаговое решение"), (3, "Промпт от модели"), (4, "Панель экспертов")):
        assert recording_console.contains(title)
    assert recording_console.contains("волк")
    assert recording_console.contains("капуст")
    assert recording_console.contains("Как перевезти всё на другой берег")
    assert client.calls == []


def test_logictask_esc_cancels_without_requests(make_app, monkeypatch):
    monkeypatch.setattr(keyboard, "read_key", lambda: keyboard.ESC)
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    from core import logictask

    client = FakeClient()
    make_app(["/logictask", "Обычный вопрос", "/exit"], client).run()

    # Esc не породил ни одного запроса задачи; обычный вопрос после отмены обработан штатно.
    assert len(client.calls) == 1
    assert logictask.LOGIC_TASK not in client.calls[0]["user"]
    assert "Обычный вопрос" in client.calls[0]["user"]


def test_logictask_strategy3_runs_both_steps(make_app, recording_console, monkeypatch):
    """Выбор стратегии 3: составленный моделью промпт используется во втором запросе."""
    keys = iter([keyboard.DOWN, keyboard.DOWN, keyboard.ENTER, keyboard.ESC])
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    client = FakeClient(answers=["СОСТАВЛЕННЫЙ ПРОМПТ", "Решение по промпту"])
    make_app(["/logictask", "/exit"], client).run()

    assert len(client.calls) == 2
    assert client.calls[1]["system"] == "СОСТАВЛЕННЫЙ ПРОМПТ"
    assert recording_console.contains("СОСТАВЛЕННЫЙ ПРОМПТ")


def test_logictask_strategy4_runs_three_experts(make_app, monkeypatch):
    keys = iter(
        [keyboard.DOWN, keyboard.DOWN, keyboard.DOWN, keyboard.ENTER, keyboard.ESC]
    )
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    from core import logictask

    client = FakeClient(answers=["Р1", "Р2", "Р3"])
    make_app(["/logictask", "/exit"], client).run()

    assert len(client.calls) == 3
    assert [c["system"] for c in client.calls] == list(logictask.EXPERT_ROLES)


def test_logictask_ignores_answer_settings(make_app, monkeypatch):
    """JSON-формат и объём 80 слов не влияют на запросы прогона: ни инструкций, ни потолка токенов."""
    keys = iter([keyboard.ENTER, keyboard.ESC])
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    client = FakeClient(["Ответ стратегии"])
    app = make_app(["/logictask", "/exit"], client)
    app.settings = AnswerSettings().with_format(AnswerFormat.JSON).with_max_words(80)
    app.run()

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["max_tokens"] == config.max_tokens_for_words(config.DEFAULT_MAX_WORDS)
    assert "JSON" not in call["system"] + call["user"]
    assert "слов" not in call["user"]
    assert "вариант" not in call["user"]


def test_logictask_run_is_not_in_history(make_app, history, history_path, monkeypatch):
    keys = iter([keyboard.ENTER, keyboard.ESC])
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    app = make_app(["/logictask", "/exit"], FakeClient(["Ответ стратегии"]))
    app.run()

    assert history.dialogues == []
    assert json.loads(history_path.read_text(encoding="utf-8")) == []
    assert app.session_count == 0


def test_logictask_error_stops_step_but_session_continues(
    make_app, recording_console, history, monkeypatch
):
    """Ошибка на втором шаге стратегии 3: остаток не выполняется, сессия живёт."""
    keys = iter([keyboard.DOWN, keyboard.DOWN, keyboard.ENTER, keyboard.ESC])
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    class TwoStepClient(FakeClient):
        def ask(self, system_message, user_message, max_tokens=0, temperature=None):
            self.calls.append({"system": system_message, "user": user_message})
            if len(self.calls) == 2:
                raise DeepseekAPIError("Тестовая ошибка API.")
            return "СОСТАВЛЕННЫЙ ПРОМПТ"

    client = TwoStepClient()
    app = make_app(["/logictask", "Обычный вопрос", "/exit"], client)
    app.run()

    assert len(client.calls) == 3  # 2 шага стратегии (второй упал) + обычный вопрос
    assert recording_console.contains("Тестовая ошибка API.")
    assert app.session_count == 1
    assert [d["question"] for d in history.dialogues] == ["Обычный вопрос"]


def test_logictask_is_a_known_command(make_app, recording_console):
    make_app(["/exit"], FakeClient()).run()
    assert "/logictask" in tui_app.COMMANDS
    assert recording_console.contains("Команды: /exit, /commands")


# --- /commands: панель команд и выполнение выбранной команды --------------------------------


def test_commands_panel_enter_runs_selected_clear(
    make_app, recording_console, history, history_path, monkeypatch
):
    """Выбор /clear в панели выполняет команду: история очищена, сообщение напечатано."""
    client = FakeClient()
    keys = iter([keyboard.DOWN, keyboard.DOWN, keyboard.DOWN, keyboard.DOWN, keyboard.ENTER])  # /clear
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    history.add("Вопрос", "Ответ")
    make_app(["/commands", "/exit"], client).run()

    assert "История диалога очищена." in recording_console.text
    assert history.dialogues == []
    assert json.loads(history_path.read_text(encoding="utf-8")) == []
    assert client.calls == []  # панель не делает запросов к модели


def test_commands_panel_enter_exit_terminates_app(make_app, recording_console, monkeypatch):
    """Выбор /exit в панели завершает приложение с сохранением истории."""
    keys = iter([keyboard.ENTER])  # первая строка панели — /exit
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    app = make_app(["/commands"])
    app.run()

    assert app._exit_requested  # завершение именно командой из панели, а не EOF
    assert recording_console.contains(tui_app.GOODBYE_MESSAGE)


def test_commands_panel_enter_settings_opens_screen(make_app, monkeypatch):
    """Выбор /settings открывает экран настроек — тот же обработчик, что при ручном наборе."""
    opened = []
    monkeypatch.setattr(TabletopAITUI, "_open_settings_screen", lambda self: opened.append(True))
    keys = iter([keyboard.DOWN, keyboard.DOWN, keyboard.ENTER])  # /settings
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    make_app(["/commands", "/exit"]).run()

    assert opened == [True]


def test_commands_panel_esc_executes_nothing(
    make_app, recording_console, history, monkeypatch
):
    """Esc закрывает панель: ничего не выполнено, сессия продолжается."""
    keys = iter([keyboard.ESC])
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    make_app(["/commands", "/exit"]).run()

    assert recording_console.contains(tui_app.GOODBYE_MESSAGE)  # вышли только через /exit
    for command in ("История диалога очищена.",):
        assert command not in recording_console.text


def test_commands_is_a_known_command_and_autocomplete_sees_it():
    assert "/commands" in tui_app.COMMANDS
    assert tui_app.COMMANDS[1] == "/commands"


def test_status_bar_hint_lists_only_exit_and_commands(make_app, recording_console):
    make_app(["/exit"], FakeClient()).run()
    output = recording_console.text
    assert "Команды: /exit, /commands" in output
    assert "Команды: /exit, /commands, /settings" not in output
    assert "/clear" not in output.split("Команды: ")[-1]
    assert "/logictask" not in output.split("Команды: ")[-1]

