"""Главный цикл приложения с подставленными консолью, историей и клиентом."""

import contextlib
import json
from pathlib import Path
from typing import List, Optional

import pytest

from core import config, context_compressor, context_strategies
from core.answer_settings import AnswerFormat, AnswerSettings, ContextStrategy
from core.api_client import AnswerMeta, APIError
from core.history_manager import HistoryManager
from ui import branches_screen, keyboard, settings_screen, tui_app
from ui.tui_app import TabletopAITUI


class FakeClient:
    """Подставной клиент: отдаёт заготовленные ответы и запоминает, что у него спросили.

    temperature=None означает «вызывающий не передал температуру» — так отличают
    вспомогательный запрос стратегии (дефолт клиента) от явной передачи настройки.
    """

    def __init__(self, answers=None, error: Optional[Exception] = None, usages=None,
                 finish_reason: Optional[str] = None) -> None:
        self.answers = list(answers or ["Ответ по умолчанию"])
        self.error = error
        # Список (prompt_tokens, completion_tokens, cost_usd) — по одному на вызов;
        # последний повторяется, если вызовов больше.
        self.usages = list(usages) if usages else None
        self.finish_reason = finish_reason
        self.calls: List[dict] = []

    def ask(
        self,
        system_message: str,
        user_message: str,
        max_tokens: int = 0,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> str:
        self.calls.append(
            {
                "system": system_message,
                "user": user_message,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "model": model,
            }
        )
        if self.error is not None:
            raise self.error
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]

    def ask_with_usage(
        self,
        system_message: str,
        user_message: str,
        max_tokens: int = 0,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> AnswerMeta:
        content = self.ask(system_message, user_message, max_tokens, temperature, model)
        if self.usages:
            prompt, completion, cost = self.usages.pop(0) if len(self.usages) > 1 else self.usages[0]
        else:
            prompt, completion, cost = 10, 20, 0.0001
        return AnswerMeta(
            content=content,
            model=model or config.DEFAULT_MODEL,
            elapsed_seconds=0.01,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cost_usd=cost,
            finish_reason=self.finish_reason,
        )

    def ask_with_usage_messages(
        self,
        messages,
        max_tokens: int = 0,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> AnswerMeta:
        """Режим стека агента: system — первое сообщение, user — последний user-ход."""
        system_message = messages[0]["content"]
        user_message = [m for m in messages if m["role"] == "user"][-1]["content"]
        meta = self.ask_with_usage(
            system_message, user_message, max_tokens=max_tokens, temperature=temperature, model=model
        )
        self.calls[-1]["messages"] = [dict(m) for m in messages]
        return meta


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


def test_first_question_after_restart_carries_restored_context(make_app, history):
    history.add("Старый вопрос", "Старый ответ")
    client = FakeClient()
    make_app(["Новый вопрос", "/exit"], client).run()

    messages = client.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert "Старый вопрос" in messages[1]["content"]
    assert messages[2]["content"] == "Старый ответ"


def test_clear_after_restart_resets_restored_context(make_app, history):
    history.add("Старый вопрос", "Старый ответ")
    client = FakeClient()
    make_app(["/clear", "Новый вопрос", "/exit"], client).run()

    messages = client.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "Старый вопрос" not in messages[1]["content"]


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

    record = history.dialogues[0]
    assert record["question"] == "Вопрос про Splendor"
    assert record["answer"] == "Ответ"
    assert record["usage"]["total_tokens"] == 30
    assert json.loads(history_path.read_text(encoding="utf-8"))["dialogues"][0]["question"] == (
        "Вопрос про Splendor"
    )


def test_status_bar_has_no_dialogue_counter(make_app, recording_console):
    """Счётчик диалогов за сессию удалён из статус-бара."""
    make_app(["Вопрос 1", "Вопрос 2", "Вопрос 3", "/exit"], FakeClient(["Ответ"])).run()

    assert "Диалогов за сессию" not in recording_console.text
    assert recording_console.contains("Сессия: 90 ток.")




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
    client = FakeClient(error=APIError("Неверный API-ключ."))
    app = make_app(["Вопрос", "/exit"], client)
    app.run()

    assert recording_console.contains("Неверный API-ключ.")
    assert recording_console.contains("Попробуйте повторить запрос.")
    assert history.dialogues == []


def test_app_survives_an_error_and_answers_the_next_question(make_app, recording_console, history):
    class FlakyClient(FakeClient):
        def ask(self, system_message, user_message, max_tokens=0, temperature=None, model=None):
            self.calls.append({"user": user_message})
            if len(self.calls) == 1:
                raise APIError("Ошибка соединения с API.")
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
    assert json.loads(history_path.read_text(encoding="utf-8")) == {
        "summary": None,
        "summary_covers": 0,
        "facts": {},
        "dialogues": [],
    }
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
        _settings_keys(
            [settings_screen.ROW_TEMPERATURE],
            *[keyboard.BACKSPACE] * 3,
            "1", ".", "2", keyboard.ESC,
        )
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
        _settings_keys(
            [(settings_screen.ROW_FORMAT, keyboard.RIGHT), settings_screen.ROW_MAX_WORDS],
            *[keyboard.BACKSPACE] * 3,
            "5", "0",
            *[keyboard.DOWN],
            keyboard.BACKSPACE, "6", keyboard.ESC,
        )
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
    keys = iter(
        _settings_keys(
            [settings_screen.ROW_MAX_WORDS],
            *[keyboard.BACKSPACE] * 3, "9", "9", "9", "9", keyboard.ESC,
        )
    )
    monkeypatch.setattr(keyboard, "read_key", lambda: next(keys))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)

    app = make_app(["/settings", "/exit"], FakeClient())
    app.run()

    assert app.settings.max_words == config.DEFAULT_MAX_WORDS
    assert recording_console.contains(f"{config.MIN_MAX_WORDS}..{config.MAX_MAX_WORDS}")


def test_settings_change_affects_the_next_request(make_app, monkeypatch):
    """Главное следствие /settings: изменившийся промпт и потолок токенов в следующем запросе."""
    keys = iter(
        _settings_keys(
            [settings_screen.ROW_MAX_WORDS],
            *[keyboard.BACKSPACE] * 3, "8", "0",
            *[keyboard.DOWN],
            keyboard.BACKSPACE, "2", keyboard.ESC,
        )
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


# --- температура запроса ----------------------------------------------------------------------


def test_question_carries_temperature_setting(make_app):
    """Температура настройки сессии доходит до клиента с каждым вопросом."""
    client = FakeClient()
    app = make_app(["Вопрос", "/exit"], client)
    app.settings = AnswerSettings().with_temperature(1.2)
    app.run()
    assert client.calls[0]["temperature"] == 1.2


def test_removed_logictask_command_is_sent_as_a_question(make_app, recording_console):
    """Удалённая команда больше не обрабатывается: ввод с «/» уходит модели как вопрос."""
    client = FakeClient()
    app = make_app(["/logictask", "/exit"], client)
    app.run()

    assert len(client.calls) == 1
    assert "/logictask" in client.calls[0]["user"]


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
    assert json.loads(history_path.read_text(encoding="utf-8")) == {
        "summary": None,
        "summary_covers": 0,
        "facts": {},
        "dialogues": [],
    }
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
    assert "/context" not in output.split("Команды: ")[-1]


# --- /models: панель выбора модели ----------------------------------------------------------


def _panel_keys(monkeypatch, keys) -> None:
    key_iter = iter(keys)
    monkeypatch.setattr(keyboard, "read_key", lambda: next(key_iter))
    monkeypatch.setattr(keyboard, "raw_mode", _noop_context)


def _settings_keys(rows, *tail) -> list:
    """Клавиши экрана настроек: дойти до строк с начала экрана, затем остальное.

    Строки задаются их именами (`settings_screen.ROW_*`), поэтому новая строка экрана не
    переписывает тесты клавиатурной навигации.
    """
    keys, current = [], settings_screen.ROW_FORMAT
    for row in rows:
        if isinstance(row, tuple):
            row, *extra = row
            while current != row:
                keys.append(keyboard.DOWN)
                current += 1
            keys.extend(extra)
        else:
            while current != row:
                keys.append(keyboard.DOWN)
                current += 1
    keys.extend(tail)
    return keys


def test_models_command_selects_session_model(make_app, monkeypatch):
    _panel_keys(monkeypatch, [keyboard.DOWN, keyboard.ENTER])

    client = FakeClient()
    app = make_app(["/models", "/exit"], client)
    app.run()

    assert app.model == config.AVAILABLE_MODELS[1]
    assert client.calls == []  # панель не делает запросов к модели


def test_models_command_esc_keeps_current_model(make_app, monkeypatch):
    _panel_keys(monkeypatch, [keyboard.DOWN, keyboard.ESC])

    client = FakeClient()
    app = make_app(["/models", "/exit"], client)
    app.run()

    assert app.model == config.DEFAULT_MODEL
    assert client.calls == []


def test_question_uses_selected_model(make_app, monkeypatch):
    _panel_keys(monkeypatch, [keyboard.DOWN, keyboard.DOWN, keyboard.DOWN, keyboard.ENTER])  # glm-5.1

    client = FakeClient()
    make_app(["/models", "Вопрос", "/exit"], client).run()

    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "glm-5.1"


def test_strategy_follows_the_selected_model(make_app, monkeypatch):
    """Вспомогательный запрос стратегии уходит с моделью сессии."""
    _panel_keys(monkeypatch, [keyboard.DOWN, keyboard.ENTER])  # deepseek-v4-pro

    client = FakeClient(["Ответ"])
    make_app(["/models", "Вопрос", "/exit"], client).run()

    assert len(client.calls) == 1
    assert client.calls[0]["model"] == config.AVAILABLE_MODELS[1]


def test_removed_logictask_is_not_a_known_command(make_app, recording_console):
    make_app(["/exit"], FakeClient()).run()
    assert "/logictask" not in tui_app.COMMANDS
    assert "/context" in tui_app.COMMANDS
    assert recording_console.contains("Команды: /exit, /commands")


def test_models_is_a_known_command_and_autocomplete_sees_it():
    assert "/models" in tui_app.COMMANDS
    assert tui_app.COMMANDS[3] == "/models"


def test_status_bar_shows_current_model(make_app, recording_console):
    make_app(["/exit"], FakeClient()).run()
    assert f"Модель: {config.DEFAULT_MODEL}" in recording_console.text


# --- день 8: предупреждения об усечении, /usage, итоги в статус-баре -------------------


def test_empty_answer_with_length_finish_reason_warns_about_budget(make_app, recording_console):
    """Reasoning-модель исчерпала max_tokens: content пуст, ошибки нет — нужен видимый излом."""
    client = FakeClient(answers=[""], finish_reason="length", usages=[(100, 4050, 0.01)])
    make_app(["Вопрос", "/exit"], client).run()

    assert recording_console.contains("исчерпала бюджет")


def test_nonempty_answer_with_length_finish_reason_warns_about_truncation(make_app, recording_console):
    client = FakeClient(answers=["Обрезанный ответ"], finish_reason="length")
    make_app(["Вопрос", "/exit"], client).run()

    assert recording_console.contains("мог быть обрезан")


def test_normal_answer_has_no_truncation_warning(make_app, recording_console):
    make_app(["Вопрос", "/exit"], FakeClient()).run()

    assert not recording_console.contains("мог быть обрезан")
    assert not recording_console.contains("исчерпал бюджет")


def test_context_command_prints_context_state_without_requests(
    make_app, recording_console
):
    """`/context` рендерит снимок агента: стратегию, окно, память и оценку токенов."""
    client = FakeClient(["Ответ про Каркассон"])
    app = make_app(["Вопрос про Каркассон", "/context", "/exit"], client)
    app.run()

    assert len(client.calls) == 1  # отчёт не обращается к модели
    assert recording_console.contains("Состояние контекста")
    assert recording_console.contains("Стратегия: резюме")
    assert recording_console.contains("В ближайшем запросе")
    assert recording_console.contains("Резюме: пока нет")
    assert recording_console.contains("Ветки: ветка 1 — активная")
    assert recording_console.contains("Оценка запроса")


def test_context_command_after_clear_reports_empty_state(make_app, recording_console):
    app = make_app(["/clear", "/context", "/exit"], FakeClient())
    app.run()
    assert recording_console.contains("из 0 обменов лога сессии")


def test_context_is_an_autocomplete_command():
    assert "/context" in tui_app.COMMANDS


def test_usage_command_prints_report_after_question(make_app, recording_console):
    client = FakeClient()
    make_app(["Вопрос", "/usage", "/exit"], client).run()

    assert recording_console.contains("Последний запрос")
    assert recording_console.contains("Сессия")
    assert recording_console.contains("Всего диалога")
    assert not recording_console.contains("Окно контекста")  # состояние контекста — в /context
    assert len(client.calls) == 1  # отчёт не обращается к модели


def test_usage_command_before_any_question_reports_nothing_yet(make_app, recording_console):
    make_app(["/usage", "/exit"], FakeClient()).run()

    assert recording_console.contains("пока не было запросов")


def test_usage_command_is_known_and_autocomplete_sees_it():
    assert "/usage" in tui_app.COMMANDS


def test_status_bar_shows_session_totals_after_answers(make_app, recording_console):
    make_app(["Вопрос 1", "Вопрос 2", "/exit"], FakeClient()).run()

    assert recording_console.contains("Сессия: 60 ток.")


def test_status_bar_session_totals_are_zero_before_first_question(make_app, recording_console):
    make_app(["/exit"], FakeClient()).run()

    assert recording_console.contains("Сессия: 0 ток.")


def test_status_bar_session_totals_unknown_cost_when_unpriced(make_app, recording_console):
    client = FakeClient(usages=[(10, 20, None)])
    make_app(["Вопрос", "/exit"], client).run()

    assert recording_console.contains("Сессия: 30 ток.")
    assert recording_console.contains("неизвестно")


def test_status_bar_shows_model_after_selection(make_app, recording_console, monkeypatch):
    _panel_keys(monkeypatch, [keyboard.DOWN, keyboard.ENTER])

    app = make_app(["/models", "/exit"], FakeClient())
    app.run()

    assert f"Модель: {config.AVAILABLE_MODELS[1]}" in recording_console.text



# --- день 9: строка о сжатии и фаза индикатора ----------------------------------------


def test_compression_line_printed_when_threshold_reached(make_app, recording_console):
    """После срабатывания порога в журнале есть строка о сжатии; до порога её нет."""
    client = FakeClient(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6"]
    )
    make_app(["Вопрос 1", "Вопрос 2", "Вопрос 3", "Вопрос 4", "Вопрос 5", "Вопрос 6", "/exit"], client).run()

    assert recording_console.contains("Контекст сжат: 8 сообщений (4 обменов) → резюме")


def test_no_compression_line_without_compression(make_app, recording_console):
    make_app(["Вопрос 1", "/exit"], FakeClient()).run()
    assert "Контекст сжат" not in recording_console.text


def test_compression_line_not_in_history_file(make_app, history_path):
    """Строка о сжатии — только экран: в history.json её нет."""
    client = FakeClient(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6"]
    )
    make_app(["Вопрос 1", "Вопрос 2", "Вопрос 3", "Вопрос 4", "Вопрос 5", "Вопрос 6", "/exit"], client).run()

    data = json.loads(history_path.read_text(encoding="utf-8"))
    assert data["summary"] == "РЕЗЮМЕ 1"
    assert all("Контекст сжат" not in json.dumps(d) for d in data["dialogues"])


def test_compression_line_printed_even_when_question_fails(make_app, recording_console, history):
    """Резюме могло уйти, а вопрос упасть: строка о сжатии всё равно печатается."""
    class FlakyClient(FakeClient):
        def __init__(self):
            super().__init__(answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "НЕВАЖНО"])
            self.questions = 0

        def ask(self, system_message, user_message, max_tokens=0, temperature=None, model=None):
            self.calls.append({"system": system_message, "user": user_message})
            if system_message == context_compressor.summary_instruction():
                return "РЕЗЮМЕ 1"
            self.questions += 1
            if self.questions > 5:
                raise APIError("Сбой API.")
            return self.answers.pop(0)

    app = make_app(
        ["Вопрос 1", "Вопрос 2", "Вопрос 3", "Вопрос 4", "Вопрос 5", "Вопрос 6", "/exit"],
        FlakyClient(),
    )
    app.run()

    assert recording_console.contains("Контекст сжат: 8 сообщений (4 обменов) → резюме")
    assert recording_console.contains("Сбой API.")
    assert len(history.dialogues) == 5  # вопросы 1-5 сохранены, шестой не дошёл


def test_spinner_label_shows_summarization_during_compression(make_app, monkeypatch):
    """Фаза сжатия переключает подпись индикатора на «Суммаризация...»."""
    labels = []

    class StatusStub:
        def __init__(self, label: str) -> None:
            labels.append(label)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def update(self, label: str) -> None:
            labels.append(label)

    class PhaseClient(FakeClient):
        def ask(self, system_message, user_message, max_tokens=0, temperature=None, model=None):
            if system_message == context_compressor.summary_instruction():
                assert labels[-1] == "● Суммаризация..."
                self.calls.append({"system": system_message, "user": user_message})
                return "РЕЗЮМЕ 1"
            return super().ask(system_message, user_message, max_tokens, temperature, model)

    app = make_app(
        ["Вопрос 1", "Вопрос 2", "Вопрос 3", "Вопрос 4", "Вопрос 5", "Вопрос 6", "/exit"],
        PhaseClient(answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6"]),
    )
    monkeypatch.setattr(app.console, "status", lambda label, **kwargs: StatusStub(label))
    app.run()

    assert labels.count("● Суммаризация...") == 1
    assert "● Отправка..." in labels[0]




# --- день 10: панели и строки журнала для стратегий -----------------------------------------


def test_branches_panel_creates_and_switches_branches(make_app, recording_console, monkeypatch):
    """`c` — чекпоинт, `n` — новая ветка от него; панель не делает запросов к модели."""
    client = FakeClient(["Ответ 1", "Ответ 2"])
    _panel_keys(
        monkeypatch,
        # каждая панель закрывается своим исходом: чекпоинт, новая ветка, переключение
        [branches_screen.KEY_CHECKPOINT, branches_screen.KEY_NEW_BRANCH, keyboard.UP, keyboard.ENTER],
    )
    app = make_app(["/branches", "/branches", "/branches", "/exit"], client)
    app.run()

    assert app.agent.active_branch == "ветка 1"
    assert [name for name, _ in app.agent.branches] == ["ветка 1", "ветка 2"]
    assert client.calls == []


def test_branches_panel_esc_changes_nothing(make_app, monkeypatch):
    _panel_keys(monkeypatch, [keyboard.ESC])
    app = make_app(["/branches", "/exit"], FakeClient())
    app.run()
    assert app.agent.branches == (("ветка 1", 0),)


def test_branches_command_opens_panel_with_branch_list(
    make_app, recording_console, monkeypatch
):
    """Панель видна на экране: рендер вне Live проверяется напрямую, как у /settings."""
    _panel_keys(monkeypatch, [keyboard.ESC])
    app = make_app(["/branches", "/exit"], FakeClient())
    state = branches_screen.initial_state(app.agent.branches, app.agent.active_branch)
    recording_console.console.print(app._render_branches_panel(state))

    assert recording_console.contains("Ветки диалога")
    assert recording_console.contains("ветка 1")
    assert recording_console.contains("(активная)")


def test_facts_line_reports_an_update_and_stays_out_of_history(
    make_app, recording_console, history, history_path, monkeypatch
):
    """Строка о фактах — только экран: в history.json её нет."""
    class FactsClient(FakeClient):
        def ask_with_usage_messages(self, messages, **kwargs):
            if messages[0]["content"] == context_strategies.facts_instruction():
                self.calls.append({"facts": True})
                return self._meta('{"цель": "собрать ТЗ"}', kwargs.get("model"))
            return super().ask_with_usage_messages(messages, **kwargs)

        def _meta(self, content, model):
            return AnswerMeta(
                content=content,
                model=model or config.DEFAULT_MODEL,
                elapsed_seconds=0.01,
                prompt_tokens=5,
                completion_tokens=5,
                total_tokens=10,
                cost_usd=0.00001,
            )

    app = make_app(["Первый вопрос", "/exit"], FactsClient(["Ответ 1"]))
    app.settings = app.settings.with_context_strategy(ContextStrategy.STICKY_FACTS)
    app.run()

    assert recording_console.contains("Факты обновлены: 1 ключей")
    data = json.loads(history_path.read_text(encoding="utf-8"))
    assert data["facts"] == {"цель": "собрать ТЗ"}
    assert all("Факты обновлены" not in json.dumps(d) for d in data["dialogues"])


def test_facts_line_reports_a_failed_update(make_app, recording_console, monkeypatch):
    class BrokenFactsClient(FakeClient):
        def ask_with_usage_messages(self, messages, **kwargs):
            if messages[0]["content"] == context_strategies.facts_instruction():
                raise APIError("Извлекатель недоступен.")
            return super().ask_with_usage_messages(messages, **kwargs)

    app = make_app(["Первый вопрос", "/exit"], BrokenFactsClient(["Ответ 1"]))
    app.settings = app.settings.with_context_strategy(ContextStrategy.STICKY_FACTS)
    app.run()

    assert recording_console.contains("Факты не обновлены")
    assert recording_console.contains("Ответ 1")  # ответ при этом напечатан


def test_spinner_label_shows_facts_update_phase(make_app, monkeypatch):
    """Фаза обновления фактов переключает подпись индикатора."""
    labels = []

    class StatusStub:
        def __init__(self, label: str) -> None:
            labels.append(label)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def update(self, label: str) -> None:
            labels.append(label)

    class FactsClient(FakeClient):
        def ask_with_usage_messages(self, messages, **kwargs):
            if messages[0]["content"] == context_strategies.facts_instruction():
                return AnswerMeta(
                    content="{}",
                    model=config.DEFAULT_MODEL,
                    elapsed_seconds=0.01,
                    prompt_tokens=5,
                    completion_tokens=5,
                    total_tokens=10,
                    cost_usd=0.00001,
                )
            return super().ask_with_usage_messages(messages, **kwargs)

    app = make_app(["Вопрос", "/exit"], FactsClient(["Ответ"]))
    app.settings = app.settings.with_context_strategy(ContextStrategy.STICKY_FACTS)
    monkeypatch.setattr(app.console, "status", lambda label, **kwargs: StatusStub(label))
    monkeypatch.setattr(tui_app, "TYPING_DELAY", 0)
    app.run()

    assert "● Обновление фактов..." in labels
    assert "● Отправка..." in labels


def test_switching_strategy_shows_in_the_status_bar(make_app, recording_console):
    app = make_app(["/exit"], FakeClient())
    app.settings = app.settings.with_context_strategy(ContextStrategy.BRANCHING)
    app.run()
    assert recording_console.contains("Стратегия: ветки")
