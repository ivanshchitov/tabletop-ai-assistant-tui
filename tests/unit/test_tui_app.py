"""Главный цикл приложения с подставленными консолью, историей и клиентом."""

import contextlib
import json
from pathlib import Path
from typing import List, Optional

import pytest

from core import config, context_compressor, context_strategies
from core.answer_settings import AnswerFormat, AnswerSettings, ContextStrategy
from core.api_client import AnswerMeta, APIError
from core import tabletop_agent
from core.history_manager import HistoryManager
from core.long_term_memory import LongTermMemory
from core import task_state
from core.schedule_store import ScheduleStore
from core.task_state import TaskStore
from core.user_profile import ProfileStore
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
def isolated_long_term_memory(tmp_path, monkeypatch):
    """Долговременная память, профиль и очередь задач всегда указывают на временные файлы."""
    monkeypatch.setattr(
        tabletop_agent, "LongTermMemory", lambda: LongTermMemory(tmp_path / "memory.json")
    )
    monkeypatch.setattr(
        tabletop_agent, "ProfileStore", lambda: ProfileStore(tmp_path / "profile.json")
    )
    monkeypatch.setattr(tabletop_agent, "TaskStore", lambda: TaskStore(tmp_path / "task.json"))
    monkeypatch.setattr(
        tabletop_agent, "ScheduleStore", lambda: ScheduleStore(tmp_path / "schedule.json")
    )


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
def no_mcp_servers(monkeypatch):
    """По умолчанию реестр MCP пуст: приложение подключается к серверам при запуске, и без
    этой заглушки каждый тест интерфейса поднимал бы настоящие серверы реестра — то есть лез
    бы в сеть. Тест, которому серверы нужны, задаёт их сам через `fake_registry`.
    """
    monkeypatch.setattr(config, "mcp_servers", tuple)


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

    # Смена контракта (add-agent-invariants): вторым идёт сообщение инвариантов.
    messages = client.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "system", "user", "assistant", "user"]
    assert "Старый вопрос" in messages[2]["content"]
    assert messages[3]["content"] == "Старый ответ"


def test_clear_after_restart_resets_restored_context(make_app, history):
    history.add("Старый вопрос", "Старый ответ")
    client = FakeClient()
    make_app(["/clear", "Новый вопрос", "/exit"], client).run()

    messages = client.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "system", "user"]
    assert "Старый вопрос" not in messages[2]["content"]


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
        "working": {},
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
        "working": {},
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
    _panel_keys(monkeypatch, [keyboard.DOWN, keyboard.DOWN, keyboard.ENTER])  # glm-5.3-flash

    client = FakeClient()
    make_app(["/models", "Вопрос", "/exit"], client).run()

    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "glm-5.3-flash"


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
    # Смена контракта (add-mcp-startup-connections): первая подпись индикатора — подключение к
    # MCP при запуске, поэтому подпись запроса ищется по всему списку, а не в labels[0].
    assert any("● Отправка..." in label for label in labels)




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


# --- день 11: /memory и журнальные строки слоёв ----------------------------------------


def test_memory_command_reports_three_layers_without_requests(make_app, recording_console):
    """Отчёт строится из снимка агента: слои, хранилища и правила маршрута — без запросов."""
    client = FakeClient(["Ответ"])
    make_app(["/memory", "/exit"], client).run()

    assert client.calls == []
    assert recording_console.contains("Память агента")
    assert recording_console.contains("Краткосрочная")
    assert recording_console.contains("Рабочая")
    assert recording_console.contains("Долговременная")
    assert recording_console.contains("history.json")
    assert recording_console.contains("memory.json")
    assert recording_console.contains("Правила маршрутизации")


def test_memory_command_shows_records_of_each_layer(make_app, recording_console):
    app = make_app(
        ["Я опытный игрок, у меня больше 300 партий. Собери партию на вечер.", "/memory", "/exit"],
        FakeClient(),
    )
    app.run()

    assert recording_console.contains("профиль · опыт")
    assert recording_console.contains("цель · цель")


def test_memory_command_after_clear_keeps_the_long_term_layer(make_app, recording_console):
    app = make_app(
        ["Я опытный игрок, у меня больше 300 партий.", "/clear", "/memory", "/exit"],
        FakeClient(),
    )
    app.run()

    assert recording_console.contains("профиль · опыт")
    assert recording_console.contains("Краткосрочная: 0 обменов диалога")


def test_memory_goal_subcommand_writes_the_working_layer(make_app, recording_console):
    app = make_app(["/memory goal Подобрать игру на вечер", "/exit"], FakeClient())
    app.run()

    assert recording_console.contains("Рабочая память: цель")
    assert app.agent.working_memory["цель"] == "Подобрать игру на вечер"


def test_memory_remember_subcommand_writes_the_long_term_layer(make_app, recording_console):
    app = make_app(["/memory remember Я не люблю игры с таймером", "/exit"], FakeClient())
    app.run()

    assert recording_console.contains("Долговременная память: профиль · предпочтения")
    assert app.agent.long_term_memory.get("предпочтения") is not None


def test_memory_forget_subcommand_removes_one_record(make_app, recording_console):
    app = make_app(
        ["Я опытный игрок, у меня больше 300 партий.", "/memory forget опыт", "/exit"],
        FakeClient(),
    )
    app.run()

    assert recording_console.contains("Удалено из долговременной памяти: опыт")
    assert app.agent.long_term_memory.records() == ()


def test_memory_forget_unknown_key_is_reported(make_app, recording_console):
    make_app(["/memory forget нет такого", "/exit"], FakeClient()).run()

    assert recording_console.contains("Записи «нет такого» нет ни в одном слое")


def test_memory_forget_all_keeps_the_dialogue_but_wipes_the_layers(make_app, recording_console):
    app = make_app(
        ["Я опытный игрок, у меня больше 300 партий. Собери партию на вечер.",
         "/memory forget all", "/exit"],
        FakeClient(),
    )
    app.run()

    assert recording_console.contains("Краткосрочная память")
    assert app.agent.long_term_memory.records() == ()
    assert app.agent.working_memory == {}
    assert app.agent.memory_report().short_term_exchanges == 1


def test_unknown_memory_subcommand_shows_the_hint(make_app, recording_console):
    make_app(["/memory что-то", "/exit"], FakeClient()).run()

    assert recording_console.contains("/memory goal")


def test_routing_line_names_the_layers_that_got_records(make_app, recording_console):
    make_app(["Я опытный игрок, у меня больше 300 партий. Собери партию на вечер.", "/exit"], FakeClient()).run()

    assert recording_console.contains("долговременная (профиль: опыт)")
    assert recording_console.contains("рабочая (цель)")


def test_routing_line_is_absent_without_matched_turns(make_app, recording_console):
    make_app(["Какие правила у Каркассона?", "/exit"], FakeClient()).run()

    assert not recording_console.contains("Память:")


def test_clear_says_what_survived_and_how_to_remove_it(make_app, recording_console):
    """Сообщение /clear перечисляет, что осталось: память снимает /memory, профиль — /profile."""
    make_app(["/clear", "/exit"], FakeClient()).run()

    assert recording_console.contains("долговременная память и профиль пользователя сохранены")
    assert recording_console.contains("/memory forget all")
    assert recording_console.contains("/profile forget")


def test_memory_is_a_known_command_and_autocomplete_sees_it(make_app, recording_console):
    make_app(["/exit"], FakeClient()).run()  # список команд — источник и панели, и Tab

    assert "/memory" in tui_app.COMMANDS


def test_plural_ru_picks_the_right_form():
    """Отчёт печатает числа словами: «1 обмен», «2 обмена», «5 обменов» — без «1 обменов»."""
    assert tui_app.plural_ru(0, "обмен", "обмена", "обменов") == "0 обменов"
    assert tui_app.plural_ru(1, "обмен", "обмена", "обменов") == "1 обмен"
    assert tui_app.plural_ru(2, "обмен", "обмена", "обменов") == "2 обмена"
    assert tui_app.plural_ru(4, "обмен", "обмена", "обменов") == "4 обмена"
    assert tui_app.plural_ru(5, "обмен", "обмена", "обменов") == "5 обменов"
    assert tui_app.plural_ru(11, "обмен", "обмена", "обменов") == "11 обменов"
    assert tui_app.plural_ru(21, "обмен", "обмена", "обменов") == "21 обмен"
    assert tui_app.plural_ru(22, "запись", "записи", "записей") == "22 записи"
    assert tui_app.plural_ru(112, "запись", "записи", "записей") == "112 записей"


def test_memory_report_uses_correct_plural_forms(make_app, recording_console):
    app = make_app(
        [
            "Я опытный игрок, у меня больше 300 партий. Собери партию на вечер без таймера.",
            "/memory",
            "/exit",
        ],
        FakeClient(),
    )
    app.run()

    assert recording_console.contains("Краткосрочная: 1 обмен диалога")
    assert recording_console.contains("Долговременная: 1 запись")
    assert recording_console.contains("Рабочая: 2 записи")
    assert not recording_console.contains("1 обменов")
    assert not recording_console.contains("1 записей")


# --- день 12: /profile и диалог настройки -----------------------------------------------


def script_interview(monkeypatch, answers, interrupt_after: Optional[int] = None):
    """Ответы диалога настройки читаются из sys.stdin.readline — как ручной ввод ключа."""
    reader = iter(answers)
    state = {"reads": 0}

    class FakeStdin:
        def readline(self):
            if interrupt_after is not None and state["reads"] >= interrupt_after:
                raise KeyboardInterrupt
            state["reads"] += 1
            try:
                return next(reader) + "\n"
            except StopIteration:
                raise EOFError  # исчерпание ввода = Ctrl+D

    monkeypatch.setattr("sys.stdin", FakeStdin())


NOVICE_ANSWERS = ["новичок", "коротко и просто", "только игры до часа", "новичок", "евро"]


def test_profile_command_reports_no_active_profile(make_app, recording_console):
    client = FakeClient(["Ответ"])
    make_app(["/profile", "/exit"], client).run()

    assert client.calls == []
    assert recording_console.contains("Профиль пользователя")
    assert recording_console.contains("Активного профиля нет")
    assert recording_console.contains("profile.json")
    assert recording_console.contains("/profile setup")


def test_profile_setup_asks_five_questions_in_order(make_app, recording_console, monkeypatch):
    script_interview(monkeypatch, NOVICE_ANSWERS)
    make_app(["/profile setup", "/exit"], FakeClient(["Ответ"])).run()

    assert recording_console.contains("Вопрос 1 из 5. Как вас зовут?")
    assert recording_console.contains(
        "Вопрос 2 из 5. Как отвечать: коротко и просто или развёрнуто?"
    )
    assert recording_console.contains("Вопрос 5 из 5. Ваши любимые жанры и механики")
    assert recording_console.contains("Профиль: Стиль — коротко и просто")
    assert recording_console.contains("Профиль «новичок» сохранён и активен")


def test_profile_setup_does_not_call_the_model(make_app, recording_console, monkeypatch):
    """Настройка профиля — диалог с приложением: ни одного запроса к API."""
    script_interview(monkeypatch, NOVICE_ANSWERS)
    client = FakeClient(["Ответ"])
    make_app(["/profile setup", "/exit"], client).run()

    assert client.calls == []


def test_profile_setup_reports_a_skipped_section(make_app, recording_console, monkeypatch):
    script_interview(monkeypatch, ["новичок", "", "", "", ""])
    make_app(["/profile setup", "/exit"], FakeClient(["Ответ"])).run()

    assert recording_console.contains("Профиль: Стиль — без изменений")
    assert recording_console.contains("Профиль «новичок» сохранён и активен")


def test_profile_report_shows_sections_and_all_profiles(make_app, recording_console, monkeypatch):
    script_interview(monkeypatch, NOVICE_ANSWERS)
    make_app(["/profile setup", "/exit"], FakeClient(["Ответ"])).run()

    script_interview(monkeypatch, ["эксперт", "развёрнуто", "тяжёлые стратегии", "клуб", "евро"])
    app = make_app(["/profile setup", "/profile", "/exit"], FakeClient(["Ответ"]))
    app.run()

    assert recording_console.contains("Активный профиль: эксперт")
    assert recording_console.contains("Стиль: развёрнуто")
    assert recording_console.contains("Профили файла: новичок, эксперт")
    assert recording_console.contains("уходит системным сообщением в каждый вопрос")
    assert recording_console.contains("/profile use <имя>")


def test_profile_setup_cancelled_by_keyboard_keeps_the_profile(make_app, recording_console, monkeypatch):
    script_interview(monkeypatch, ["новичок", "коротко и просто"], interrupt_after=2)
    app = make_app(["/profile setup", "/profile", "/exit"], FakeClient(["Ответ"]))
    app.run()

    assert recording_console.contains("Настройка профиля отменена")
    assert recording_console.contains("Активного профиля нет")
    assert app.agent.profile_report().names == ()


def test_profile_use_switches_the_active_profile(make_app, recording_console, monkeypatch):
    script_interview(monkeypatch, NOVICE_ANSWERS)
    make_app(["/profile setup", "/exit"], FakeClient(["Ответ"])).run()

    script_interview(monkeypatch, ["эксперт", "", "", "", ""])
    app = make_app(["/profile setup", "/profile use новичок", "/profile", "/exit"], FakeClient(["Ответ"]))
    app.run()

    assert recording_console.contains("Профиль «новичок» теперь активен")
    assert recording_console.contains("Активный профиль: новичок")


def test_profile_use_unknown_name_is_reported(make_app, recording_console):
    make_app(["/profile use эксперт", "/exit"], FakeClient(["Ответ"])).run()

    assert recording_console.contains("Профиля «эксперт» нет")


def test_profile_forget_removes_the_profile(make_app, recording_console, monkeypatch):
    script_interview(monkeypatch, NOVICE_ANSWERS)
    app = make_app(["/profile setup", "/profile forget новичок", "/profile", "/exit"], FakeClient(["Ответ"]))
    app.run()

    assert recording_console.contains("Профиль «новичок» удалён")
    assert app.agent.profile_report().names == ()
    assert recording_console.contains("Профили файла: нет")


def test_unknown_profile_subcommand_shows_the_hint(make_app, recording_console):
    make_app(["/profile что-то", "/exit"], FakeClient(["Ответ"])).run()

    assert recording_console.contains("/profile setup")


def test_question_carries_the_profile_after_setup(make_app, recording_console, monkeypatch):
    script_interview(monkeypatch, NOVICE_ANSWERS)
    client = FakeClient(["Ответ"])
    make_app(["/profile setup", "Подбери, во что нам поиграть вечером", "/exit"], client).run()

    messages = client.calls[0]["messages"]
    assert "Профиль пользователя «новичок»" in messages[1]["content"]
    assert "Стиль: коротко и просто" in messages[1]["content"]


def test_profile_is_a_known_command_and_autocomplete_sees_it(make_app, recording_console):
    make_app(["/exit"], FakeClient(["Ответ"])).run()

    assert "/profile" in tui_app.COMMANDS


def test_status_bar_shows_the_active_profile(make_app, recording_console, monkeypatch):
    """Активный профиль виден после каждого шага, а без профиля статус-бар прежний."""
    script_interview(monkeypatch, NOVICE_ANSWERS)
    make_app(["/profile setup", "Вопрос", "/exit"], FakeClient(["Ответ"])).run()

    assert recording_console.contains("Модель: deepseek-v4.1-flash  |  Профиль: новичок  |  Формат:")


def test_status_bar_hides_the_profile_when_there_is_none(make_app, recording_console):
    make_app(["/exit"], FakeClient(["Ответ"])).run()

    assert not recording_console.contains("Профиль:")


def test_profile_text_with_markup_characters_does_not_break_the_report(
    make_app, recording_console, monkeypatch
):
    """Значение профиля — текст пользователя: скобочные последовательности печатаются как есть."""
    script_interview(monkeypatch, ["[/]имя", "[/dim] и ещё [скобки]"])
    make_app(["/profile setup", "/profile", "/exit"], FakeClient(["Ответ"])).run()

    assert recording_console.contains("[/dim] и ещё [скобки]")


# --- команда /task: отчёт, очередь и прогон ---

PLAN_JSON_TUI = '{"items": ["Тема", "Ход", "Очки"]}'
OK_JSON_TUI = '{"ok": true, "issues": []}'


def _task_app(make_app, client, inputs, monkeypatch, keys=(), typed=()):
    """Приложение с подменённым терминалом прогона.

    `keys` — ответы на опрос клавиши паузы (пустая строка — «паузы нет»), `typed` — нажатия,
    которые сценарий вводит в строку правок (последним считается Enter, если его не передали).
    """
    monkeypatch.setattr(tui_app.keyboard, "raw_mode", _noop_context)
    pending = list(keys)

    def read_key_nowait():
        return pending.pop(0) if pending else ""

    typed_keys = list(typed)

    def read_key():
        return typed_keys.pop(0) if typed_keys else keyboard.ENTER

    monkeypatch.setattr(tui_app.keyboard, "read_key_nowait", read_key_nowait)
    monkeypatch.setattr(tui_app.keyboard, "read_char", read_key)
    return make_app(inputs, client)


def test_task_command_is_known_and_autocompletes(make_app, recording_console):
    make_app(["/exit"], FakeClient()).run()

    assert "/task" in tui_app.COMMANDS


def test_task_report_on_the_empty_queue_does_not_ask_the_model(make_app, recording_console):
    client = FakeClient()
    make_app(["/task", "/exit"], client).run()

    assert client.calls == []
    assert recording_console.contains("Задача агента")
    assert recording_console.contains("Этапы: Planning → Execution → Validation → Done")
    assert recording_console.contains("Очередь задач пуста")
    assert recording_console.contains("task.json")


def test_task_add_and_stop_manage_the_queue(make_app, recording_console):
    client = FakeClient()
    app = make_app(
        [
            "/task add",
            "/task add Собрать подборку на вечер",
            "/task add Разработать игру про улиток",
            "/task stop",
            "/task",
            "/exit",
        ],
        client,
    )
    app.run()

    assert recording_console.contains("Цель не указана")
    assert recording_console.contains("Задача в очереди (1 из 1)")
    assert recording_console.contains("Задача в очереди (2 из 2)")
    assert recording_console.contains("Очередь задач снята")
    assert recording_console.contains("Очередь задач пуста")
    assert client.calls == []


def _app_with_plan(make_app, app_goals=("Тема", "Ход", "Очки")):
    app = make_app(["/exit"], FakeClient())
    app.agent.add_task("Разработать игру про улиток")
    app.agent.task.save(
        task_state.add_section(
            task_state.accept_plan(
                task_state.plan_built(task_state.begin_task(app.agent.task.state), app_goals)
            ),
            "Раздел артефакта",
        )
    )
    return app


def test_task_report_shows_the_plan_with_marks_and_the_artifact(make_app, recording_console):
    app = _app_with_plan(make_app, ("Тема", "Ход"))

    app._handle_task("/task")

    assert recording_console.contains("Очередь (1)")
    assert recording_console.contains("▸ 1. Разработать игру про улиток — Execution, 2/2: «Ход»")
    assert recording_console.contains("Текущая задача: «Разработать игру про улиток»")
    assert recording_console.contains("Этап: Execution (2 из 4)")
    assert recording_console.contains("Текущий шаг: 2/2: «Ход»")
    assert recording_console.contains("Ожидаемое действие: выполнить подзадачу «Ход»")
    assert recording_console.contains("[x] Тема")
    assert recording_console.contains("[ ] Ход")
    assert recording_console.contains("Артефакт: 1 раздел, 16 символов")
    assert recording_console.contains("Пауза: нет")


def test_task_report_lists_the_plan_one_item_per_line(make_app, recording_console):
    app = _app_with_plan(make_app)

    app._handle_task("/task")

    lines = [line.strip() for line in recording_console.text.splitlines()]
    assert "План:" in lines
    assert "[x] Тема" in lines
    assert "[ ] Ход" in lines


def test_task_report_after_all_tasks_are_done(make_app, recording_console):
    """Очередь без незавершённых задач: отчёт печатает задачи и объём артефактов, без падения."""
    app = _app_with_plan(make_app)
    app.agent.task.save(
        task_state.validation_verdict(
            task_state.start_validation(
                task_state.add_section(
                    task_state.add_section(app.agent.task.state, "Второй раздел"),
                    "Третий раздел",
                )
            ),
            [],
        )
    )

    app._handle_task("/task")

    chars = len("Раздел артефакта") + len("Второй раздел") + len("Третий раздел")
    assert recording_console.contains("Очередь (1)")
    assert recording_console.contains(f"решена, артефакт {chars} символа")
    assert recording_console.contains("Незавершённых задач нет — прогон остановлен")


def test_task_report_survives_markup_in_the_goal(make_app, recording_console):
    """Цель задачи — текст пользователя: скобочные последовательности печатаются как есть."""
    app = make_app(["/exit"], FakeClient())
    app.agent.add_task("[/dim] игра со [скобками]")

    app._handle_task("/task")

    assert recording_console.contains("[/dim] игра со [скобками]")


def test_task_panel_shows_stages_step_action_and_plan(make_app, recording_console):
    app = _app_with_plan(make_app, ("Тема", "Ход"))

    recording_console.console.print(app._render_task_panel(app.agent.task_report()))

    assert recording_console.contains("Задача 1/1: «Разработать игру про улиток»")
    assert recording_console.contains("✓ Planning")
    assert recording_console.contains("▸ Execution")
    assert recording_console.contains("Текущий шаг: 2/2: «Ход»")
    assert recording_console.contains("Ожидаемое действие: выполнить подзадачу «Ход»")
    assert recording_console.contains("[x] Тема")
    assert recording_console.contains("[ ] Ход")


def test_task_panel_lists_the_plan_one_item_per_line(make_app, recording_console):
    """План в панели — список: подзадачи на разных строках, а не одна длинная строка."""
    app = _app_with_plan(make_app)

    recording_console.console.print(app._render_task_panel(app.agent.task_report()))

    lines = recording_console.text.splitlines()
    done_lines = [line for line in lines if "[x] Тема" in line]
    todo_lines = [line for line in lines if "[ ] Ход" in line]
    assert done_lines and todo_lines
    assert done_lines[0] != todo_lines[0]


def test_task_panel_shows_the_question_with_the_typed_answer(make_app, recording_console):
    """Пока ждут правки, панель остаётся на экране и показывает саму строку ввода."""
    app = _app_with_plan(make_app)

    recording_console.console.print(
        app._render_task_panel(app.agent.task_report(), answer="добавь подсчёт очков")
    )

    assert recording_console.contains(
        "Правки к плану (Enter — принять, Ctrl+C — пауза): добавь подсчёт очков"
    )


def test_task_panel_hints_how_to_pause_on_every_frame(make_app, recording_console):
    """Подсказка о паузе — на каждом кадре панели: и во время запроса, и при вопросе о правках."""
    app = _app_with_plan(make_app)

    for panel in (
        app._render_task_panel(app.agent.task_report()),
        app._render_task_panel(app.agent.task_report(), "Выполнение подзадачи..."),
        app._render_task_panel(app.agent.task_report(), answer="добавь подсчёт"),
    ):
        before = recording_console.text
        recording_console.console.print(panel)
        assert "Пауза — клавиша p или Ctrl+C" in recording_console.text[len(before):]

    assert recording_console.contains("Выполнение подзадачи...")


def test_status_bar_shows_the_task_while_the_queue_is_full(make_app, recording_console):
    make_app(["/task add Разработать игру", "/exit"], FakeClient()).run()

    assert recording_console.contains("Задача: 1/1 «Разработать игру» — Planning")


def test_status_bar_hides_the_task_without_a_queue(make_app, recording_console):
    make_app(["/exit"], FakeClient()).run()

    assert not recording_console.contains("Задача:")


def test_task_run_drives_the_whole_queue_to_done(make_app, recording_console, monkeypatch):
    client = FakeClient([PLAN_JSON_TUI, "Раздел 1", "Раздел 2", "Раздел 3", OK_JSON_TUI])
    app = _task_app(
        make_app, client, ["/task add Разработать игру", "/task run", "/exit"], monkeypatch
    )

    app.run()

    assert recording_console.contains("Правок нет — план принят.")
    assert recording_console.contains("Итог: «Разработать игру» — задача решена: 3 раздела артефакта")
    assert recording_console.contains("Результат: ")  # файл результата назван в журнале
    assert recording_console.contains("Очередь задач пуста — прогон остановлен")
    assert app.agent.task.state.tasks[0].status.value == "решена"
    assert [call["model"] for call in client.calls] == [config.DEFAULT_MODEL] * 5


def test_task_run_echoes_the_edits_answer(make_app, recording_console, monkeypatch):
    """Ответ на вопрос о правках печатается строкой: прогон идёт в режиме без отражения ввода."""
    client = FakeClient(
        [
            PLAN_JSON_TUI,
            '{"items": ["Правила", "Компоненты", "Плейтест"]}',
            "Раздел 1",
            "Раздел 2",
            "Раздел 3",
            OK_JSON_TUI,
        ]
    )
    app = _task_app(
        make_app,
        client,
        ["/task add Игра", "/task run", "/exit"],
        monkeypatch,
        typed=list("добавь пункт про подсчёт") + [keyboard.ENTER],
    )

    app.run()

    assert recording_console.contains("Правки: добавь пункт про подсчёт")
    request = client.calls[1]["messages"][-1]["content"]
    assert "добавь пункт про подсчёт" in request
    assert "1. Тема" in request  # прежний план ушёл в запрос
    assert "Артефакт:" in client.calls[-1]["messages"][-1]["content"]  # последний запрос — проверка


def test_task_run_prints_the_status_bar_with_the_task_line(make_app, recording_console, monkeypatch):
    """После прогона статус-бар печатается как обычно — со строкой задачи в нём."""
    client = FakeClient([PLAN_JSON_TUI, "Раздел 1", "Раздел 2", "Раздел 3", OK_JSON_TUI])
    app = _task_app(
        make_app, client, ["/task add Разработать игру", "/task run", "/exit"], monkeypatch
    )

    app.run()

    assert recording_console.text.count("Задача: 1/1 «Разработать игру»") >= 1


def test_task_run_pauses_on_the_key_and_continues_from_the_same_step(
    make_app, recording_console, monkeypatch
):
    client = FakeClient([PLAN_JSON_TUI, "Раздел 1", "Раздел 2", "Раздел 3", OK_JSON_TUI])
    app = _task_app(
        make_app,
        client,
        ["/task add Разработать игру", "/task run", "/task", "/task run", "/exit"],
        monkeypatch,
        keys=("", "", "p"),
    )

    app.run()

    assert recording_console.contains("⏸ Пауза: Execution")
    assert recording_console.contains("Пауза: да")
    assert recording_console.contains("Пауза снята — продолжаю с сохранённого шага")
    assert recording_console.contains("Очередь задач пуста — прогон остановлен")
    assert app.agent.task.state.finished is True
    # После паузы конвейер продолжил со второй подзадачи: первая уже в артефакте.
    resumed = client.calls[2]["messages"][-1]["content"]
    assert "Твоя подзадача: 2. Ход" in resumed


def test_task_run_pauses_on_ctrl_c_without_exiting_the_app(
    make_app, recording_console, monkeypatch
):
    client = FakeClient([PLAN_JSON_TUI, "Раздел 1", "Раздел 2", "Раздел 3", OK_JSON_TUI])
    calls = {"count": 0}

    def read_key_nowait():
        calls["count"] += 1
        if calls["count"] >= 2:
            raise KeyboardInterrupt
        return ""

    monkeypatch.setattr(tui_app.keyboard, "raw_mode", _noop_context)
    monkeypatch.setattr(tui_app.keyboard, "read_key_nowait", read_key_nowait)
    monkeypatch.setattr(tui_app.keyboard, "read_char", lambda: keyboard.ENTER)
    app = make_app(["/task add Игра", "/task run", "/task", "/exit"], client)

    app.run()

    assert recording_console.contains("⏸ Пауза:")
    assert recording_console.contains("Пауза: да")
    assert app.agent.tasks_paused is True
    assert recording_console.contains("До встречи")


def test_task_run_without_a_queue_prints_the_hint(make_app, recording_console, monkeypatch):
    client = FakeClient()
    app = _task_app(make_app, client, ["/task run", "/exit"], monkeypatch)

    app.run()

    assert recording_console.contains("Очередь задач пуста — поставьте задачу")
    assert client.calls == []


def test_unknown_task_subcommand_prints_the_hint(make_app, recording_console):
    make_app(["/task что-то", "/exit"], FakeClient()).run()

    assert recording_console.contains("/task add <цель>")


def test_task_panel_shows_what_the_spend_was_for(make_app, recording_console):
    """Траты прогона — в панели, с пометкой, на какой запрос они ушли."""
    app = _app_with_plan(make_app)
    meta = AnswerMeta(
        content="",
        model="deepseek-v4.1-flash",
        elapsed_seconds=12.5,
        prompt_tokens=100,
        completion_tokens=200,
        total_tokens=300,
        cost_usd=0.001,
    )
    app._task_spend = [("Execution, 2/3: «Ход»", meta), ("Validation, попытка 1/2", meta)]

    recording_console.console.print(app._render_task_panel(app.agent.task_report()))

    assert recording_console.contains("Последний запрос: Validation, попытка 1/2 — 12.5с, 300 ток., $0.0010")
    assert recording_console.contains("За прогон: 2 запроса, 600 ток., $0.0020")


def test_task_run_frame_shows_the_standard_input_field(make_app, recording_console):
    """Поле ввода во время прогона — то же приглашение, что у главного цикла."""
    app = _app_with_plan(make_app)
    app._run_input = "подбери игру"

    recording_console.console.print(app._run_frame())

    assert recording_console.contains("> Введите вопрос (или /exit для выхода): подбери игру")


def test_typed_line_pauses_the_run_and_is_handled_by_the_main_loop(
    make_app, recording_console, monkeypatch
):
    """Enter с набранной строкой останавливает прогон, а строку обрабатывает главный цикл."""
    client = FakeClient([PLAN_JSON_TUI, "Раздел 1", "Раздел 2", "Раздел 3", OK_JSON_TUI])
    app = _task_app(
        make_app,
        client,
        ["/task add Игра", "/task run", "/task stop", "/exit"],
        monkeypatch,
        # Набор приходит между операциями: строку собирает поле ввода прогона.
        keys=("", "", "", "/", "t", "a", "s", "k", keyboard.ENTER),
    )

    app.run()
    assert recording_console.contains("⏸ Пауза:")
    assert recording_console.contains("Очередь задач снята")  # строка дошла до главного цикла
    assert app.agent.task.state.tasks == ()


# --- день 14: /invariants и строки о нарушении ------------------------------------------


def test_invariants_report_lists_the_table_without_api_calls(make_app, recording_console):
    client = FakeClient(["Ответ"])
    app = make_app(["/invariants", "/exit"], client)
    app.run()

    assert client.calls == []
    assert recording_console.contains("Инварианты агента")
    assert recording_console.contains("1. Только физические компоненты")
    assert recording_console.contains("6. Только официальные правила издателя")
    assert recording_console.contains("проверка кодом по словам: приложени")
    assert recording_console.contains("только модель")
    assert recording_console.contains("системным сообщением в каждый вопрос")
    assert recording_console.contains("конвейер")


def test_invariants_arguments_are_ignored(make_app, recording_console):
    client = FakeClient(["Ответ"])
    make_app(["/invariants forget all", "/exit"], client).run()

    assert client.calls == []
    assert recording_console.contains("1. Только физические компоненты")


def test_clean_answer_prints_nothing_about_invariants(make_app, recording_console):
    make_app(["Что взять на вечер?", "/exit"], FakeClient(["Берите Каркассон."])).run()

    assert not recording_console.contains("⛔")
    assert not recording_console.contains("Инвариант")
    assert not recording_console.contains("нвариант")  # ни в статус-баре, ни в журнале


def test_violation_prints_the_retry_line_and_the_clean_retry(make_app, recording_console):
    client = FakeClient(["Берите Монополию!", "Берите Каркассон."])
    make_app(["Что взять на вечер?", "/exit"], client).run()

    assert len(client.calls) == 2
    assert recording_console.contains("⛔ Инвариант 3 нарушен («монопол») — повторный запрос")
    assert recording_console.contains("Берите Каркассон.")
    assert not recording_console.contains("Ответ отклонён")


def test_second_violation_prints_the_rejection_and_the_app_refusal(make_app, recording_console):
    client = FakeClient(["Берите Монополию!", "Ну возьмите монополию.", "Чисто."])
    make_app(["Что взять на вечер?", "/exit"], client).run()

    assert recording_console.contains("⛔ Инвариант 3 нарушен («монопол») — повторный запрос")
    assert recording_console.contains("⛔ Ответ отклонён: инвариант 3")
    assert recording_console.contains("Не могу предложить: ответ нарушает инвариант 3")
    assert not recording_console.contains("Ну возьмите")


def test_rejected_answer_does_not_trigger_the_json_warning(make_app, recording_console):
    client = FakeClient(["Берите Монополию!", "Ну возьмите монополию.", "Чисто."])
    app = make_app(["Что взять на вечер?", "/exit"], client)
    app.settings = AnswerSettings().with_format(AnswerFormat.JSON)
    app.run()

    assert recording_console.contains("⛔ Ответ отклонён")
    assert not recording_console.contains("Модель не вернула валидный JSON")


# --- контролируемые переходы: /task stage и журнал в отчёте ---


def _put_task_in_execution(app, goal: str = "Разработать игру") -> None:
    """Задача с утверждённым планом на этапе Execution — исходное состояние для переходов."""
    assert app.agent.add_task(goal) is True
    app.agent.task.save(
        task_state.accept_plan(
            task_state.plan_built(task_state.begin_task(app.agent.task.state), ("Тема", "Ход"))
        )
    )


def test_task_stage_prints_the_refusal(make_app, recording_console):
    """Недопустимый переход отклонён: на экране причина и разрешённые переходы, запросов нет."""
    client = FakeClient()
    app = make_app(["/task stage done", "/exit"], client)
    _put_task_in_execution(app)

    app.run()

    assert recording_console.contains("Переход отклонён")
    assert recording_console.contains("Execution → Done")
    assert recording_console.contains("Разрешены: Validation, Planning")
    assert app.agent.task.state.active.stage is task_state.Stage.EXECUTION
    assert client.calls == []


def test_task_stage_performs_an_allowed_transition(make_app, recording_console):
    app = make_app(["/task stage planning", "/exit"], FakeClient())
    _put_task_in_execution(app)

    app.run()

    assert recording_console.contains("Переход выполнен: Execution → Planning")
    assert app.agent.task.state.active.stage is task_state.Stage.PLANNING


def test_task_stage_without_a_name_lists_the_stages(make_app, recording_console):
    make_app(["/task add Игра", "/task stage", "/exit"], FakeClient()).run()

    assert recording_console.contains("Planning, Execution, Validation, Done")


def test_task_stage_without_a_task_says_so(make_app, recording_console):
    make_app(["/task stage execution", "/exit"], FakeClient()).run()

    assert recording_console.contains("незавершённой задачи нет")


def test_task_report_prints_the_transition_journal(make_app, recording_console):
    app = make_app(["/task stage done", "/task", "/exit"], FakeClient())
    _put_task_in_execution(app)

    app.run()

    assert recording_console.contains("Разрешённые переходы: Validation, Planning")
    assert recording_console.contains("Журнал переходов:")
    assert recording_console.contains("✓ Planning → Execution")
    assert recording_console.contains("✗ Execution → Done")


def test_task_panel_shows_the_last_transition(make_app, recording_console):
    """В панели прогона видно, каким переходом задача попала на текущий этап."""
    app = make_app(["/exit"], FakeClient())
    _put_task_in_execution(app)

    recording_console.console.print(app._render_task_panel(app.agent.task_report()))

    assert recording_console.contains("Planning → Execution")


# --- день 16: /mcp ----------------------------------------------------------------------


FAKE_MCP_SERVER = Path(__file__).resolve().parent.parent / "fake_mcp_server.py"


@pytest.fixture
def fake_mcp(monkeypatch):
    """Реестр прогона — один фейковый сервер.

    Смена контракта (add-mcp-startup-connections): приложение подключается ко всем записям
    реестра при запуске, поэтому тесты задают реестр, а не команду запуска одного сервера.
    """

    def use(*flags: str, name: str = "фейковый") -> None:
        fake_registry(monkeypatch, fake_spec(name, *flags))

    return use


def test_mcp_report_prints_server_and_tools(make_app, recording_console, fake_mcp):
    fake_mcp()
    client = FakeClient(["Ответ"])
    make_app(["/mcp", "/exit"], client).run()

    assert client.calls == []
    assert recording_console.contains("фейковый-сервер")
    assert recording_console.contains("9.9.9")
    assert recording_console.contains("2025-06-18")
    assert recording_console.contains("fake_mcp_server.py")
    assert recording_console.contains("fake_search")
    assert recording_console.contains("Поиск по фейковому каталогу")
    assert recording_console.contains("fake_details")


def test_mcp_arguments_are_ignored(make_app, recording_console, fake_mcp):
    fake_mcp()
    client = FakeClient(["Ответ"])
    make_app(["/mcp tools", "/exit"], client).run()

    assert client.calls == []
    assert recording_console.contains("fake_search")


def test_mcp_is_offered_by_completion_and_panel():
    from ui import commands_screen

    assert "/mcp" in tui_app.COMMANDS
    assert "/mcp" in [command for command, _ in commands_screen.COMMAND_OPTIONS]


def test_mcp_failure_prints_reason_and_session_continues(
    make_app, recording_console, monkeypatch
):
    fake_registry(monkeypatch, fake_spec("фейковый", command="нет-такой-команды-на-диске"))
    client = FakeClient(["Ответ про настолки"])
    make_app(["/mcp", "Вопрос про настолки", "/exit"], client).run()

    assert recording_console.contains("Не удалось подключиться")
    # Сессия продолжается: следующий ввод обработан как обычный вопрос.
    assert len(client.calls) == 1


def test_mcp_report_escapes_server_text(make_app, recording_console, fake_mcp):
    """Имена и описания приходят от чужого процесса: rich принял бы [x] за разметку."""
    fake_mcp("--markup")
    make_app(["/mcp", "/exit"], FakeClient()).run()

    assert recording_console.contains("fake_[x]_tool")
    assert recording_console.contains("[/dim]")


def test_mcp_report_without_tools(make_app, recording_console, fake_mcp):
    fake_mcp("--empty")
    make_app(["/mcp", "/exit"], FakeClient()).run()

    assert recording_console.contains("фейковый-сервер")
    assert recording_console.contains("инструментов не объявлено")


def fake_registry(monkeypatch, *specs) -> None:
    from core import config as core_config

    monkeypatch.setattr(core_config, "mcp_servers", lambda: specs)


def fake_spec(name: str, *flags: str, command: Optional[str] = None):
    import sys

    from core import config as core_config

    return core_config.MCPServerSpec(
        name=name,
        transport="stdio",
        command=command or sys.executable,
        args=(str(FAKE_MCP_SERVER),) + flags,
        env_keys=(),
        description=f"фейковый сервер {name}",
    )


def test_startup_connects_to_every_server_and_prints_the_summary(
    make_app, recording_console, monkeypatch
):
    fake_registry(monkeypatch, fake_spec("первый"), fake_spec("второй"))
    make_app(["/exit"], FakeClient()).run()

    assert recording_console.contains("MCP: 2/2")
    # Два сервера по два инструмента: строка итога говорит про инструменты, а не про серверы одни.
    assert recording_console.contains("4")


def test_startup_survives_an_unavailable_server(make_app, recording_console, monkeypatch):
    fake_registry(
        monkeypatch,
        fake_spec("рабочий"),
        fake_spec("сломанный", command="нет-такой-команды-на-диске"),
    )
    client = FakeClient(["Ответ про настолки"])
    make_app(["Вопрос про настолки", "/exit"], client).run()

    assert recording_console.contains("MCP: 1/2")
    assert recording_console.contains("1 недоступно")
    # Сессия работает: вопрос после старта ушёл модели. Запросов два: смена контракта
    # (add-mcp-tool-calls) — при доступных инструментах вопросу предшествует запрос выбора
    # инструмента. Отключается `/tool auto off`.
    assert len(client.calls) == 2


def test_mcp_report_covers_every_server(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("первый"), fake_spec("второй", "--empty"))
    make_app(["/mcp", "/exit"], FakeClient()).run()

    assert recording_console.contains("первый")
    assert recording_console.contains("второй")
    assert recording_console.contains("fake_search")
    assert recording_console.contains("инструментов не объявлено")


def test_mcp_report_uses_the_startup_snapshot(make_app, recording_console, monkeypatch):
    """Отчёт печатается по снимку запуска: процессы серверов заново не поднимаются."""
    fake_registry(monkeypatch, fake_spec("первый"))
    app = make_app(["/mcp", "/exit"], FakeClient())

    connects = []
    original = app.agent.connect_mcp_servers

    def counting(*args, **kwargs):
        connects.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(app.agent, "connect_mcp_servers", counting)
    app.run()

    # Ровно один обход — стартовый; команда сходила в снимок.
    assert len(connects) == 1
    assert recording_console.contains("fake_search")


def test_mcp_refresh_reconnects(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("первый"))
    app = make_app(["/mcp refresh", "/exit"], FakeClient())

    connects = []
    original = app.agent.connect_mcp_servers

    def counting(*args, **kwargs):
        connects.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(app.agent, "connect_mcp_servers", counting)
    app.run()

    # Стартовый обход плюс обход по refresh.
    assert len(connects) == 2
    assert recording_console.contains("fake_search")


def test_mcp_other_arguments_print_the_snapshot(make_app, recording_console, monkeypatch):
    """Смена контракта (add-mcp-startup-connections): значим только аргумент refresh."""
    fake_registry(monkeypatch, fake_spec("первый"))
    app = make_app(["/mcp tools", "/exit"], FakeClient())

    connects = []
    original = app.agent.connect_mcp_servers
    monkeypatch.setattr(
        app.agent, "connect_mcp_servers", lambda *a, **k: connects.append(1) or original(*a, **k)
    )
    app.run()

    assert len(connects) == 1
    assert recording_console.contains("fake_search")


def test_mcp_report_shows_the_reason_for_a_failed_server(make_app, recording_console, monkeypatch):
    fake_registry(
        monkeypatch,
        fake_spec("рабочий"),
        fake_spec("сломанный", command="нет-такой-команды-на-диске"),
    )
    client = FakeClient(["Ответ"])
    make_app(["/mcp", "Вопрос про настолки", "/exit"], client).run()

    assert recording_console.contains("сломанный")
    assert recording_console.contains("Не удалось подключиться")
    assert recording_console.contains("fake_search")
    # Два запроса вместо одного — выбор инструмента перед вопросом (add-mcp-tool-calls).
    assert len(client.calls) == 2


# --- команда /tool -----------------------------------------------------------------------

TOOL_CHOICE_ANSWER = '{"server": "рабочий", "tool": "fake_echo", "arguments": {"first": "раз"}}'
NO_TOOL_ANSWER = '{"tool": null}'


def test_tool_command_is_listed_and_completed(make_app):
    from ui.commands_screen import COMMAND_OPTIONS

    from ui.tui_app import COMMANDS

    assert any(command == "/tool" for command, _ in COMMAND_OPTIONS)
    assert "/tool" in COMMANDS


def test_tool_list_prints_tools_with_parameters(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    client = FakeClient()
    make_app(["/tool list", "/exit"], client).run()

    assert recording_console.contains("fake_echo")
    assert recording_console.contains("first")
    assert recording_console.contains("обязательный")
    assert client.calls == []


def test_tool_without_arguments_prints_the_same_list(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    make_app(["/tool", "/exit"], FakeClient()).run()

    assert recording_console.contains("fake_echo")


def test_tool_call_prints_the_result(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    client = FakeClient()
    make_app(["/tool call рабочий.fake_echo first=раз second=два", "/exit"], client).run()

    assert recording_console.contains("раз")
    assert recording_console.contains("два")
    assert client.calls == []


def test_tool_call_prints_the_reason_of_a_failure(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    make_app(["/tool call рабочий.нет-такого", "/exit"], FakeClient()).run()

    assert recording_console.contains("Вызов не удался")


def test_tool_call_without_server_name_resolves_it_by_the_tool(make_app, recording_console, monkeypatch):
    """Имя сервера необязательно: в реестре оно может содержать пробел, а команда — по словам."""
    fake_registry(monkeypatch, fake_spec("рабочий"))
    make_app(["/tool call fake_echo first=раз", "/exit"], FakeClient()).run()

    assert recording_console.contains("раз")


def test_tool_call_without_tool_name_prints_the_format(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    make_app(["/tool call", "/exit"], FakeClient()).run()

    assert recording_console.contains("/tool call")


def test_tool_call_with_broken_argument_prints_the_format(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    make_app(["/tool call рабочий.fake_echo просто-слово", "/exit"], FakeClient()).run()

    assert recording_console.contains("ключ=значение")


def test_unknown_subcommand_prints_the_format(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    make_app(["/tool нет-такой-подкоманды", "/exit"], FakeClient()).run()

    assert recording_console.contains("/tool call")


def test_tool_auto_toggles_and_prints_state(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    app = make_app(["/tool auto off", "/tool auto on", "/exit"], FakeClient())
    app.run()

    assert recording_console.contains("выключен")
    assert recording_console.contains("включён")
    assert app.agent.auto_tools is True


def test_tool_auto_off_stops_the_choice_request(make_app, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    client = FakeClient(["Ответ про настолки"])
    make_app(["/tool auto off", "Вопрос про настолки", "/exit"], client).run()

    assert len(client.calls) == 1


def test_journal_line_after_an_automatic_call(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    client = FakeClient([TOOL_CHOICE_ANSWER, "Ответ про настолки"])
    make_app(["Вопрос про настолки", "/exit"], client).run()

    assert recording_console.contains("fake_echo")
    assert recording_console.contains("first=раз")


def test_no_journal_line_when_no_tool_is_needed(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    client = FakeClient([NO_TOOL_ANSWER, "Ответ про настолки"])
    make_app(["Вопрос про настолки", "/exit"], client).run()

    assert not recording_console.contains("fake_echo —")


def test_journal_line_after_a_failed_call(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    client = FakeClient(["совершенно не JSON", "Ответ про настолки"])
    make_app(["Вопрос про настолки", "/exit"], client).run()

    assert recording_console.contains("Инструмент не вызван")
    assert recording_console.contains("Ответ про настолки")


CHAIN_ANSWER = (
    '{"steps": ['
    '{"tool": "fake_search", "arguments": {"query": "огонь"}},'
    '{"tool": "fake_echo", "arguments": {"first": "$1"}},'
    '{"tool": "fake_echo", "arguments": {"first": "$2", "second": "файл"}}'
    "]}"
)


def test_chain_prints_a_line_per_step_with_passed_volume(make_app, recording_console, monkeypatch):
    """Цепочка (день 19): строка на шаг, объём переданного равен объёму результата источника."""
    fake_registry(monkeypatch, fake_spec("рабочий"))
    client = FakeClient([CHAIN_ANSWER, "Ответ про настолки"])
    app = make_app(["Вопрос про настолки", "/exit"], client)
    app.run()

    first, second, third = app.agent.last_tool_chain
    assert recording_console.contains("🔧 1/3 рабочий.fake_search")
    assert recording_console.contains("🔧 2/3 рабочий.fake_echo")
    assert recording_console.contains("🔧 3/3 рабочий.fake_echo")
    assert recording_console.contains(f"first ← шаг 1: {len(first.text)} симв.")
    assert recording_console.contains(f"first ← шаг 2: {len(second.text)} симв.")
    assert recording_console.contains(f"→ {len(third.text)} симв.")


def test_chain_failure_names_the_step_and_stops(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    broken = (
        '{"steps": [{"tool": "fake_search", "arguments": {}},'
        '{"tool": "нет-такого", "arguments": {"first": "$1"}},'
        '{"tool": "fake_echo", "arguments": {"first": "$2"}}]}'
    )
    client = FakeClient([broken, "Ответ про настолки"])
    make_app(["Вопрос про настолки", "/exit"], client).run()

    assert recording_console.contains("🔧 2/3 шаг не выполнен")
    assert recording_console.contains("цепочка остановлена, не выполнено шагов: 1")
    assert not recording_console.contains("🔧 3/3")
    assert recording_console.contains("Ответ про настолки")


def test_chain_reports_dropped_steps(make_app, recording_console, monkeypatch):
    fake_registry(monkeypatch, fake_spec("рабочий"))
    many = ",".join('{"tool": "fake_search", "arguments": {}}' for _ in range(5))
    client = FakeClient(['{"steps": [' + many + "]}", "Ответ"])
    make_app(["Вопрос про настолки", "/exit"], client).run()

    assert recording_console.contains("🔧 4/4")
    assert recording_console.contains("Отброшено шагов сверх потолка: 1")


# --- планировщик ----------------------------------------------------------------------


def test_schedule_report_without_jobs(make_app, recording_console):
    app = make_app(["/schedule"])
    app.run()

    assert recording_console.contains("Планировщик заданий")
    assert recording_console.contains("Заданий нет")
    assert app.client.calls == []


def test_schedule_report_shows_jobs_runs_and_collected(make_app, recording_console, tmp_path):
    store = ScheduleStore(path=tmp_path / "schedule.json")
    job = store.add_job(
        tool="dnd_digest", arguments={"section": "spells"}, every_minutes=5, next_run=1000.0
    )
    store.record_run(job, at=1000.0, ok=True, summary="собрано 5, впервые: 3", collected=5, fresh=3)
    store.remember_collected("spells/2014", ["лечение", "щит"])

    app = make_app(["/schedule"])
    app.agent.schedule = store
    app.run()

    assert recording_console.contains("dnd_digest")
    assert recording_console.contains("каждые 5 мин")
    assert recording_console.contains("прогонов 1")
    assert recording_console.contains("накоплено записей: 2")


def test_schedule_report_escapes_foreign_text(make_app, recording_console, tmp_path):
    """Итог прогона — текст чужого процесса: без escape rich принял бы его за разметку."""
    store = ScheduleStore(path=tmp_path / "schedule.json")
    job = store.add_job(tool="dnd_digest", arguments={}, every_minutes=5, next_run=0.0)
    store.record_run(job, at=0.0, ok=False, summary="[/dim] отказ", collected=0, fresh=0)

    app = make_app(["/schedule"])
    app.agent.schedule = store
    app.run()

    assert recording_console.contains("[/dim] отказ")
