"""TUI-приложение Tabletop AI Assistant на базе rich."""

import os
import sys
import time
from typing import List, Optional

try:
    import readline
except ImportError:  # pragma: no cover - readline недоступен на Windows
    readline = None

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

from core import config, memory_layers, user_profile
from core.answer_settings import AnswerFormat, AnswerSettings, ContextStrategy
from core.api_client import (
    API_KEY_CHARSET_ERROR,
    AnswerMeta,
    APIClient,
    APIError,
    is_valid_api_key,
    is_valid_json_answer,
)
from core.history_manager import HistoryManager
from core.tabletop_agent import RequestPhase, TabletopAgent, TaskReport

from . import branches_screen, commands_screen, keyboard, models_screen, settings_screen
from .settings_screen import SettingsScreenState

APP_TITLE = "🎲 TABLETOP AI ASSISTANT — эксперт по настольным играм"
WELCOME_MESSAGE = (
    "🎲 Tabletop AI Assistant запущен. Задайте вопрос по настольным играм. "
    "/settings — настройки формата и объёма ответа, /clear — очистить историю, /exit — выход."
)
GOODBYE_MESSAGE = "До встречи! История диалога сохранена. 🎲"
EXIT_BEFORE_START_MESSAGE = "До встречи! 🎲"
TYPING_CHUNK_SIZE = 3
# Пауза между кадрами анимации печати. Переопределяется через окружение, чтобы e2e-прогон
# по псевдотерминалу не ждал реального времени набора на каждый ответ.
TYPING_DELAY = float(os.getenv("TABLETOP_TYPING_DELAY", "0.015"))
# Полный список команд обслуживает автодополнение и панель /commands; источник —
# ui/commands_screen.COMMAND_OPTIONS, чтобы панель и Tab не разъезжались.
COMMANDS = [command for command, _ in commands_screen.COMMAND_OPTIONS]
# Подсказка в статус-баре — только точка входа: полный список с описаниями на панели /commands.
STATUS_COMMANDS = ["/exit", "/commands"]

FORMAT_LABELS = {
    AnswerFormat.COMPACT: "компактный",
    AnswerFormat.JSON: "JSON",
    AnswerFormat.FREE: "свободный",
}

STRATEGY_LABELS = {
    ContextStrategy.SUMMARY: "резюме",
    ContextStrategy.SLIDING_WINDOW: "окно",
    ContextStrategy.STICKY_FACTS: "факты",
    ContextStrategy.BRANCHING: "ветки",
}

# Названия слоёв в родительном падеже — для сообщений об удалении записи.
MEMORY_LAYER_GENITIVE = {
    memory_layers.SHORT_TERM: "краткосрочной",
    memory_layers.WORKING: "рабочей",
    memory_layers.LONG_TERM: "долговременной",
}

# Подсказка по подкомандам профиля — одна на все случаи: отчёт, ошибка аргумента и неизвестная
# подкоманда печатают её, чтобы пользователь не гадал, что доступно.
PROFILE_HINT = (
    "Подкоманды: /profile setup — диалог настройки | /profile use <имя> — переключить профиль | "
    "/profile forget <имя> — удалить профиль"
)
PROFILE_INTERVIEW_HEADER = (
    "Настройка профиля: {questions}. Вопрос за вопросом, Enter — пропустить раздел, "
    "Ctrl+C — отменить настройку."
)
PROFILE_INTERVIEW_CANCELLED = "Настройка профиля отменена — профиль не изменён."
# Приглашение ввода ответа: без rich-разметки, как основное приглашение приложения, — иначе
# readline неверно считает ширину строки и портит её при возврате каретки.
PROFILE_ANSWER_PROMPT = "> "

# Команда /task: прогон идёт без приглашения ввода, поэтому управление — клавиша паузы и Ctrl+C.
TASK_PAUSE_KEY = "p"
TASK_HINT = (
    "Подкоманды: /task add <цель> — поставить задачу в очередь | /task run — запустить и "
    "продолжить прогон | /task stop — снять очередь"
)
TASK_EMPTY_HINT = (
    "Очередь задач пуста — поставьте задачу: /task add <что нужно сделать>, затем /task run."
)
TASK_EMPTY_EPILOGUE = "Очередь задач пуста — прогон остановлен."
# Подсказка о паузе — постоянная строка панели: как остановить задачу, видно в любой момент.
TASK_PAUSE_HINT = "Пауза — клавиша p или Ctrl+C; Enter — выполнить введённое"
TASK_PAUSE_NOTICE = "⏸ Пауза: {stage}, шаг {step} — состояние сохранено; продолжить: /task run"
TASK_EDITS_QUESTION = "Правки к плану (пустая строка — принять план и идти дальше): "
TASK_EDITS_REJECTED = "Круги планирования исчерпаны — план принят как есть."
# Подписи фаз конвейера в живой панели: те же слова, что и у спиннера вопроса.
# Приглашение ввода: та же строка, что у главного цикла, — во время прогона поле ввода
# показывается стандартным образом, ниже панели и статус-бара.
INPUT_PROMPT = "> Введите вопрос (или /exit для выхода): "
TASK_PHASE_LABELS = {
    RequestPhase.TASK_PLAN: "Планирование...",
    RequestPhase.TASK_EXECUTE: "Выполнение подзадачи...",
    RequestPhase.TASK_VALIDATE: "Проверка...",
}
# Отметки подзадач плана в панели и отчёте: выполнена, не выполнена, ещё не начата.
# Печатаются через escape(): набор [x] rich принимает за разметку и выбросил бы его из вывода.
TASK_MARK_LABELS = {"✓": "[x]", "✗": "[!]", "◦": "[ ]"}


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """«1 обмен», «2 обмена», «5 обменов»: число отчёта вместе с верной формой слова.

    Русское согласование: единственное число — только при остатке 1, кроме 11; форма «двух-четырёх»
    — при остатке 2..4, кроме 12..14; остальное — множественная. Числа в отчётах маленькие, но
    «1 записей» в кадре демо выглядит неряшливо.
    """
    if count % 10 == 1 and count % 100 != 11:
        word = one
    elif count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        word = few
    else:
        word = many
    return f"{count} {word}"


class TabletopAITUI:
    def __init__(
        self,
        console: Optional[Console] = None,
        history: Optional[HistoryManager] = None,
        client: Optional[APIClient] = None,
    ) -> None:
        """Зависимости необязательны: по умолчанию — реальные консоль, история и клиент.

        Возможность подставить свои нужна тестам (консоль в буфер, история во временном файле,
        клиент-заглушка); переданный клиент к тому же означает, что API-ключ уже есть и
        запрашивать его при старте не нужно.
        """
        self.console = console if console is not None else Console()
        self.client: Optional[APIClient] = client
        self.last_error: Optional[str] = None
        self._printed_compression = None
        self._printed_facts = None
        # Обе памяти агента — внутри агента: история передаётся ему при создании,
        # и он сам восстанавливает контекст; UI только показывает сохранённое.
        self.agent = TabletopAgent(client, history=history)
        # Ход прогона задачи: набранная в поле ввода строка, строка, которую после остановки
        # обработает главный цикл, и траты по запросам с пометкой, на что они были.
        self._run_input = ""
        self._pending_input: Optional[str] = None
        self._task_spend: List[Tuple[str, AnswerMeta]] = []
        self._exit_requested = False
        self._setup_autocomplete()

    @property
    def history(self) -> HistoryManager:
        """Память диалога принадлежит агенту; интерфейс читает её для реплея."""
        return self.agent.history

    @property
    def settings(self) -> AnswerSettings:
        """Настройки ответа живут в агенте; UI читает и меняет их через свойства."""
        return self.agent.settings

    @settings.setter
    def settings(self, value: AnswerSettings) -> None:
        self.agent.settings = value

    @property
    def model(self) -> str:
        return self.agent.model

    @model.setter
    def model(self, value: str) -> None:
        self.agent.model = value

    def _setup_autocomplete(self) -> None:
        if readline is None:
            return

        def completer(text: str, state: int) -> Optional[str]:
            matches: List[str] = [c for c in COMMANDS if c.startswith(text)]
            return matches[state] if state < len(matches) else None

        readline.set_completer(completer)
        readline.set_completer_delims(" \t\n")
        if "libedit" in (readline.__doc__ or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")

    def run(self) -> None:
        if self.client is None:
            try:
                api_key = self._ensure_api_key()
            except KeyboardInterrupt:
                # Ctrl+C во время ручного ввода API-ключа тоже должен завершать
                # приложение аккуратно, а не необработанным traceback. Диалогов
                # ещё не было, поэтому сообщение про сохранённую историю не нужно.
                self.console.print()
                self.console.print(f"[bold yellow]{EXIT_BEFORE_START_MESSAGE}[/bold yellow]")
                return
            self.client = APIClient(api_key)
            self.agent.client = self.client

        self.console.print(Panel(APP_TITLE, style="bold cyan"))
        if self.history.dialogues:
            self._print_history()
        else:
            self.console.print(f"[bold yellow]{WELCOME_MESSAGE}[/bold yellow]\n")
        self._print_status_bar()

        try:
            while True:
                try:
                    # Обычный input() без rich-разметки: readline знает точную длину
                    # приглашения и не портит его при удалении введённого текста (backspace).
                    user_input = self._next_input()
                except EOFError:
                    self._exit()
                    return

                user_input = user_input.strip()
                if not user_input:
                    continue
                if user_input == "/exit":
                    self._exit()
                    return

                if user_input.startswith("/") and self._handle_command(user_input):
                    if self._exit_requested:
                        return
                    self._print_status_bar()
                    continue

                if len(user_input) > config.MAX_INPUT_LENGTH:
                    user_input = user_input[: config.MAX_INPUT_LENGTH]
                    self.console.print(
                        f"[bold yellow]Запрос слишком длинный, обрезан до "
                        f"{config.MAX_INPUT_LENGTH} символов.[/bold yellow]"
                    )

                self._handle_question(user_input)
                self._print_status_bar()
        except KeyboardInterrupt:
            # Ctrl+C может прийти как во время input(), так и во время ожидания
            # ответа API или анимации печати — ловим его на уровне всего цикла.
            self.console.print()
            self._exit()
            return

    def _next_input(self) -> str:
        """Возвращает строку для главного цикла.

        Строка, набранная в поле ввода во время прогона задачи, обрабатывается после остановки:
        прогон на это время встаёт на паузу, состояние уже записано на диск.
        """
        if self._pending_input is not None:
            line, self._pending_input = self._pending_input, None
            self.console.print(f"{INPUT_PROMPT}{line}")
            return line
        return input(INPUT_PROMPT)

    def _read_manual_key(self) -> str:
        """Читает ключ с терминала без readline.

        readline перехватывает SIGINT (rl_catch_signals у GNU readline): Ctrl+C во время
        ручного ввода ключа не поднимал KeyboardInterrupt — приложение молча перерисовывало
        промпт и продолжало ждать. Читаем строку напрямую из stdin, тогда Ctrl+C штатно
        поднимает KeyboardInterrupt, который ловит run().
        """
        self.console.print("[bold]Введите OPENCODE_API_KEY вручную:[/bold] ")
        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line.strip()

    def _ensure_api_key(self) -> str:
        """Возвращает пригодный ключ, при необходимости спрашивая его у пользователя.

        Ключ проверяется и когда он пришёл из .env: непригодный всё равно оборвал бы первый же
        запрос, поэтому лучше сказать об этом на старте, чем после первого вопроса.
        """
        api_key = config.get_api_key()
        while not is_valid_api_key(api_key or ""):
            if api_key:
                self.console.print(f"[bold red]{API_KEY_CHARSET_ERROR}[/bold red]")
            else:
                self.console.print(
                    "[bold red]API-ключ не найден. Добавьте OPENCODE_API_KEY в файл .env[/bold red]"
                )
            entered = self._read_manual_key()
            if entered:
                config.set_api_key_runtime(entered)
                api_key = entered
        return api_key

    def _handle_command(self, user_input: str) -> bool:
        """Обрабатывает команды, кроме /exit. Возвращает True, если команда распознана."""
        command = user_input.split(maxsplit=1)[0]
        if command == "/commands":
            self._open_commands_screen()
            return True
        if command == "/settings":
            self._open_settings_screen()
            return True
        if command == "/models":
            self._open_models_screen()
            return True
        if command == "/clear":
            self.agent.reset()
            self.console.print(
                "[bold green]История диалога очищена. Краткосрочная и рабочая память пусты, "
                "долговременная память и профиль пользователя сохранены — память снимает "
                "/memory forget all, профиль — /profile forget <имя>.[/bold green]"
            )
            return True
        if command == "/usage":
            self._print_usage_report()
            return True
        if command == "/context":
            self._print_context_report()
            return True
        if command == "/memory":
            self._handle_memory(user_input)
            return True
        if command == "/profile":
            self._handle_profile(user_input)
            return True
        if command == "/task":
            self._handle_task(user_input)
            return True
        if command == "/branches":
            self._open_branches_screen()
            return True
        if command == "/invariants":
            self._print_invariants_report()
            return True
        return False

    def _open_commands_screen(self) -> None:
        """Панель команд: ↑/↓ — выбор, Enter — выполнить выбранную команду, Esc — отмена.

        Здесь только цикл «прочитать клавишу — перерисовать»; как клавиши меняют экран
        и чем заканчивается выбор, решает редьюсер `ui.commands_screen`.
        Выполнение не зависит от построчного редактирования терминала: выбранная команда
        исполняется тем же диспетчером, что и ручной набор. Повторный выбор `/commands`
        заново открывает панель (цикл, а не рекурсия).
        """
        command = "/commands"
        while command == "/commands":
            state = commands_screen.initial_state()

            with Live(console=self.console, refresh_per_second=30, transient=True) as live, keyboard.raw_mode():
                live.update(self._render_commands_panel(state))
                while True:
                    key = keyboard.read_key()
                    state = commands_screen.apply_key(state, key)
                    if state.confirmed or state.cancelled:
                        break
                    live.update(self._render_commands_panel(state))

            if not state.confirmed:
                return
            command = state.selected[0]

        if command == "/exit":
            self._exit()
            self._exit_requested = True
            return
        self._handle_command(command)

    def _render_commands_panel(self, state: commands_screen.CommandsScreenState) -> Panel:
        lines = []
        for index, (command, description) in enumerate(commands_screen.COMMAND_OPTIONS):
            if index == state.selected_index:
                lines.append(f"➤ [reverse bold]{command} — {description}[/reverse bold]")
            else:
                lines.append(f"  {command} — {description}")
        body = (
            "\n".join(lines)
            + "\n\n[dim]↑/↓ — выбор, Enter — выполнить, Esc — отмена[/dim]"
        )
        return Panel(body, title="Команды", style="cyan")

    def _open_models_screen(self) -> None:
        """Панель выбора модели: ↑/↓ — выбор, Enter — применить, Esc — отмена.

        Та же схема, что у панели команд: редьюсер `ui.models_screen` решает, как клавиши
        меняют экран, здесь только raw_mode, Live и read-key/redraw. Выбор меняет только
        локальное состояние сессии — ни одного запроса к API панель не делает.
        """
        state = models_screen.initial_state(self.model)

        with Live(console=self.console, refresh_per_second=30, transient=True) as live, keyboard.raw_mode():
            live.update(self._render_models_panel(state))
            while True:
                key = keyboard.read_key()
                state = models_screen.apply_key(state, key)
                if state.confirmed or state.cancelled:
                    break
                live.update(self._render_models_panel(state))

        if state.confirmed:
            self.model = state.selected

    def _render_models_panel(self, state: models_screen.ModelSelectionState) -> Panel:
        lines = []
        for index, model in enumerate(state.available):
            marker = "➤ " if index == state.selected_index else "  "
            highlight = "[reverse bold]" if index == state.selected_index else ""
            reset = "[/reverse bold]" if index == state.selected_index else ""
            suffix = " (текущая)" if model == state.current else ""
            lines.append(f"{marker}{highlight}{model}{reset}{suffix}")
        body = "\n".join(lines) + "\n\n[dim]↑/↓ — выбор, Enter — применить, Esc — отмена[/dim]"
        return Panel(body, title="Модель", style="cyan")

    def _open_branches_screen(self) -> None:
        """Панель веток диалога: ↑/↓ — выбор, Enter — переключить, «c» — чекпоинт, «n» — новая ветка, Esc — отмена.

        Та же схема, что у панели моделей: редьюсер `ui.branches_screen` решает, как клавиши
        меняют экран, здесь только raw_mode, Live и read-key/redraw. Панель не делает
        запросов к модели: она переключает вид активной стратегии веток и точки ветвления.
        """
        state = branches_screen.initial_state(self.agent.branches, self.agent.active_branch)

        with Live(console=self.console, refresh_per_second=30, transient=True) as live, keyboard.raw_mode():
            live.update(self._render_branches_panel(state))
            while True:
                key = keyboard.read_key()
                state = branches_screen.apply_key(state, key)
                if state.finished:
                    break
                live.update(self._render_branches_panel(state))

        if state.checkpoint_requested:
            self.agent.checkpoint()
            self.console.print("[dim]Чекпоинт поставлен в активной ветке.[/dim]")
        elif state.new_branch_requested:
            name = self.agent.new_branch()
            self.console.print(f"[bold green]Создана ветка {name}.[/bold green]")
        elif state.switched:
            self.agent.switch_branch(state.selected_name)

    def _render_branches_panel(self, state: branches_screen.BranchesScreenState) -> Panel:
        lines = []
        for index, (name, exchanges) in enumerate(state.branches):
            if index == state.selected_index:
                line = f"➤ [reverse bold]{name}[/reverse bold]"
            else:
                line = f"  {name}"
            suffix = " (активная)" if name == state.active else ""
            lines.append(f"{line} — обменов: {exchanges}{suffix}")
        body = (
            "\n".join(lines)
            + "\n\n[dim]↑/↓ — выбор, Enter — переключить, c — чекпоинт, n — новая ветка, Esc — закрыть[/dim]"
        )
        return Panel(body, title="Ветки диалога", style="cyan")

    def _open_settings_screen(self) -> None:
        """Экран настроек: ↑/↓ — выбор поля, ←/→ — формат, цифры/Backspace — числовые поля, Esc — выход.

        Здесь остаётся только цикл «прочитать клавишу — перерисовать»: как именно клавиша меняет
        экран и что происходит с введёнными значениями на выходе, решает `ui.settings_screen`.
        """
        state = settings_screen.initial_state(self.settings)

        # cbreak-режим включается один раз на весь экран, а не вокруг каждого read_key() —
        # см. пояснение в keyboard.raw_mode().
        with Live(console=self.console, refresh_per_second=30, transient=True) as live, keyboard.raw_mode():
            live.update(self._render_settings_panel(state))
            while True:
                key = keyboard.read_key()
                if key == keyboard.ESC:
                    break
                state = settings_screen.apply_key(state, key)
                live.update(self._render_settings_panel(state))

        self.settings, errors = settings_screen.apply_to_settings(state, self.settings)
        for error in errors:
            self.console.print(f"[bold red]{error}[/bold red]")

    def _render_settings_panel(self, state: SettingsScreenState) -> Panel:
        format_line = "   ".join(
            f"[reverse bold]{FORMAT_LABELS[f]}[/reverse bold]" if i == state.format_index else FORMAT_LABELS[f]
            for i, f in enumerate(settings_screen.FORMAT_VALUES)
        )
        strategy_line = "   ".join(
            f"[reverse bold]{STRATEGY_LABELS[s]}[/reverse bold]" if i == state.strategy_index else STRATEGY_LABELS[s]
            for i, s in enumerate(settings_screen.STRATEGY_VALUES)
        )
        marker_format = "➤" if state.row == settings_screen.ROW_FORMAT else " "
        marker_strategy = "➤" if state.row == settings_screen.ROW_STRATEGY else " "
        marker_words = "➤" if state.row == settings_screen.ROW_MAX_WORDS else " "
        marker_list_limit = "➤" if state.row == settings_screen.ROW_LIST_LIMIT else " "
        marker_temperature = "➤" if state.row == settings_screen.ROW_TEMPERATURE else " "
        marker_compress = "➤" if state.row == settings_screen.ROW_COMPRESS_AFTER else " "
        marker_ceiling = "➤" if state.row == settings_screen.ROW_MAX_SESSION_TOKENS else " "

        def highlighted(row: int, value: str) -> str:
            return f"[reverse bold]{value or ' '}[/reverse bold]" if state.row == row else value

        body = (
            f"{marker_format} Формат ответа: {format_line}\n"
            f"{marker_strategy} Стратегия контекста: {strategy_line}\n"
            f"{marker_words} Макс. объём ({config.MIN_MAX_WORDS}..{config.MAX_MAX_WORDS} слов): "
            f"{highlighted(settings_screen.ROW_MAX_WORDS, state.max_words_input)}\n"
            f"{marker_list_limit} Лимит вариантов в списке ({config.MIN_LIST_LIMIT}..{config.MAX_LIST_LIMIT}): "
            f"{highlighted(settings_screen.ROW_LIST_LIMIT, state.list_limit_input)}\n"
            f"{marker_temperature} Температура ({config.MIN_TEMPERATURE}..{config.MAX_TEMPERATURE}): "
            f"{highlighted(settings_screen.ROW_TEMPERATURE, state.temperature_input)}\n"
            f"{marker_compress} Сжатие после ({config.MIN_COMPRESS_AFTER}..{config.MAX_COMPRESS_AFTER} сообщений): "
            f"{highlighted(settings_screen.ROW_COMPRESS_AFTER, state.compress_after_input)}\n"
            f"{marker_ceiling} Потолок контекста ({config.MIN_MAX_SESSION_TOKENS}..{config.MAX_MAX_SESSION_TOKENS} токенов): "
            f"{highlighted(settings_screen.ROW_MAX_SESSION_TOKENS, state.max_session_tokens_input)}\n"
            "\n"
            "[dim]↑/↓ — поле, ←/→ — формат и стратегия, цифры/Backspace — числовые поля, "
            "Esc — выход и сохранение[/dim]"
        )
        return Panel(body, title="Настройки", style="cyan")

    def _print_history(self) -> None:
        for item in self.history.dialogues:
            self._print_exchange(item["question"], item["answer"])

    def _print_exchange(self, question: str, answer: str) -> None:
        self.console.print(f"[bold blue]Вы:[/bold blue] {question}")
        self.console.print("[bold magenta]Tabletop AI Assistant:[/bold magenta]")
        self.console.print(Markdown(answer))
        self.console.rule(style="dim")

    def _handle_question(self, question: str) -> None:
        self.last_error = None
        self.console.print(f"[bold blue]Вы:[/bold blue] {question}")

        with self.console.status("[bold yellow]● Отправка...[/bold yellow]", spinner="dots") as status:
            def report_phase(phase: "RequestPhase") -> None:
                if phase is RequestPhase.COMPRESSION:
                    status.update("● Суммаризация...")
                elif phase is RequestPhase.FACTS_UPDATE:
                    status.update("● Обновление фактов...")
                elif phase is RequestPhase.REQUEST:
                    status.update("● Отправка...")

            try:
                meta = self.agent.ask(question, on_phase=report_phase)
            except APIError as exc:
                self.last_error = str(exc)
                self._print_memory_line()
                self._print_compression_line()
                self._print_facts_line()
                self.console.print(f"[bold red]{exc}[/bold red]")
                self.console.print("[bold red]Попробуйте повторить запрос.[/bold red]")
                self.console.rule(style="dim")
                return

        self._print_memory_line()
        self._print_compression_line()
        self._print_facts_line()
        rejected = self._print_invariants_lines()
        answer = meta.content
        self.console.print("[bold magenta]Tabletop AI Assistant:[/bold magenta]")
        self._print_typing(answer)
        # Отказ приложения — не ответ модели: судить его по формату модели нечестно.
        if (
            self.settings.format == AnswerFormat.JSON
            and not rejected
            and not is_valid_json_answer(answer)
        ):
            self.console.print("[bold yellow]⚠ Модель не вернула валидный JSON.[/bold yellow]")
        if meta.finish_reason == "length":
            if answer:
                self.console.print(
                    "[bold yellow]⚠ Ответ мог быть обрезан: модель упёрлась в бюджет "
                    "max_tokens (finish_reason=length).[/bold yellow]"
                )
            else:
                self.console.print(
                    "[bold yellow]⚠ Модель исчерпала бюджет max_tokens — ответ не "
                    "сгенерирован (finish_reason=length). Попробуйте вопрос проще или "
                    "модель слабее в рассуждениях.[/bold yellow]"
                )
        self._print_usage_meta(meta)
        self.console.rule(style="dim")

    def _print_usage_report(self) -> None:
        """Отчёт /usage: последний запрос, итоги сессии и расход сохранённой истории.

        Состояние контекста сюда не входит — его показывает `/context`. Без запросов к модели.
        """
        self.console.print("[bold cyan]Учёт токенов (без обращения к модели):[/bold cyan]")
        last = self.agent.last_result
        if last is None:
            self.console.print("[dim]  Последний запрос: пока не было запросов.[/dim]")
        else:
            last_cost = f"${last.cost_usd:.6f}" if last.cost_usd is not None else "неизвестно"
            self.console.print(
                f"[dim]  Последний запрос: токены {last.prompt_tokens}+"
                f"{last.completion_tokens}={last.total_tokens}, стоимость {last_cost}[/dim]"
            )
        session = self.agent.session_usage
        session_cost = f"${session.cost_usd:.4f}" if session.cost_usd is not None else "неизвестно"
        self.console.print(
            f"[dim]  Сессия: запросов {session.requests}, вход {session.prompt_tokens}, "
            f"выход {session.completion_tokens}, всего {session.total_tokens}, "
            f"стоимость {session_cost}[/dim]"
        )
        lifetime = self.agent.history.total_usage()
        lifetime_cost = f"${lifetime.cost_usd:.4f}" if lifetime.cost_usd is not None else "неизвестно"
        self.console.print(
            f"[dim]  Всего диалога (файл истории): запросов {lifetime.requests}, "
            f"всего {lifetime.total_tokens}, стоимость {lifetime_cost}[/dim]"
        )


    def _print_context_report(self) -> None:
        """Отчёт /context: состояние контекста из снимка агента, без запросов к модели.

        Терминальный слой ничего не знает о внутренностях агента: он рендерит снимок —
        стратегию, границы окна и потолка, память стратегии и приближённую оценку токенов.
        """
        report = self.agent.context_report()
        self.console.print("[bold cyan]Состояние контекста (без обращения к модели):[/bold cyan]")
        self.console.print(
            f"[dim]  Стратегия: {STRATEGY_LABELS[report.strategy]}, "
            f"окно {report.window} сообщений, потолок {report.max_session_tokens} токенов[/dim]"
        )
        self.console.print(
            f"[dim]  В ближайшем запросе: {report.request_turns} ходов из "
            f"{report.log_exchanges} обменов лога сессии[/dim]"
        )
        if report.has_summary:
            self.console.print(
                f"[dim]  Резюме: {report.summary_covers} обменов свёрнуто[/dim]"
            )
        else:
            self.console.print("[dim]  Резюме: пока нет[/dim]")
        if report.facts:
            self.console.print(f"[dim]  Факты ({len(report.facts)}):[/dim]")
            for key, value in report.facts.items():
                self.console.print(f"[dim]    {key}: {value}[/dim]")
        else:
            self.console.print("[dim]  Факты: блок пуст[/dim]")
        branch_list = ", ".join(
            f"{name} ({exchanges})" for name, exchanges in report.branches
        )
        self.console.print(
            f"[dim]  Ветки: {report.branch} — активная; всего {len(report.branches)}: "
            f"{branch_list}[/dim]"
        )
        self.console.print(
            f"[dim]  Оценка запроса: ≈ {report.tokens_estimate} токенов из "
            f"{report.max_session_tokens}[/dim]"
        )

    def _handle_memory(self, user_input: str) -> None:
        """Команда /memory: без аргументов — отчёт по слоям, с аргументами — операция над слоем.

        Аргументы у команды значимы (в отличие от прочих команд приложения): подкоманда явно
        выбирает, в какой слой идёт запись. Все операции — методы агента, поэтому интерфейс
        не пишет в хранилища сам.
        """
        parts = user_input.split(maxsplit=2)
        if len(parts) == 1:
            self._print_memory_report()
            return
        subcommand = parts[1]
        argument = parts[2].strip() if len(parts) > 2 else ""
        if subcommand == "goal" and argument:
            self.agent.remember_goal(argument)
            self.console.print(f"[dim]Рабочая память: цель — {argument}[/dim]")
            return
        if subcommand == "remember" and argument:
            record = self.agent.remember(argument)
            self.console.print(
                f"[dim]Долговременная память: {record.category} · {record.key} — "
                f"{escape(record.value)}[/dim]"
            )
            return
        if subcommand == "forget" and argument:
            self._forget_memory(argument)
            return
        self.console.print(
            "[dim]Подкоманды: /memory goal <цель задачи> | /memory remember <сведение о вас> | "
            "/memory forget <ключ> | /memory forget all[/dim]"
        )

    def _forget_memory(self, key: str) -> None:
        if key == "all":
            self.agent.forget_all()
            self.console.print(
                "[bold green]Рабочая и долговременная память очищены. Краткосрочная память — "
                "ход текущего диалога — не тронута: её очищает /clear.[/bold green]"
            )
            return
        layer = self.agent.forget(key)
        if layer is None:
            self.console.print(f"[dim]Записи «{key}» нет ни в одном слое.[/dim]")
            return
        self.console.print(
            f"[dim]Удалено из {MEMORY_LAYER_GENITIVE[layer]} памяти: {key}[/dim]"
        )

    def _print_memory_report(self) -> None:
        """Отчёт /memory: три слоя из снимка агента, без запросов к модели.

        Интерфейс не знает, как слои устроены внутри: он печатает снимок агента — содержимое
        слоёв, их хранилища, срок жизни и перечень правил маршрутизации.
        """
        report = self.agent.memory_report()
        self.console.print("[bold cyan]Память агента (без обращения к модели):[/bold cyan]")
        self.console.print(
            f"[dim]  Краткосрочная: "
            f"{plural_ru(report.short_term_exchanges, 'обмен', 'обмена', 'обменов')} диалога "
            f"({memory_layers.LAYER_LIFETIME[memory_layers.SHORT_TERM]})[/dim]"
        )
        self.console.print(
            f"[dim]    хранилище: {report.history_store} "
            f"({memory_layers.LAYER_STORE_HINT[memory_layers.SHORT_TERM]})[/dim]"
        )
        self.console.print(
            f"[dim]  Рабочая: "
            f"{plural_ru(len(report.working), 'запись', 'записи', 'записей')} "
            f"({memory_layers.LAYER_LIFETIME[memory_layers.WORKING]})[/dim]"
        )
        for record in report.working:
            self.console.print(f"[dim]    {record.category} · {record.key}: {record.value}[/dim]")
        self.console.print(
            f"[dim]    хранилище: {report.history_store} "
            f"({memory_layers.LAYER_STORE_HINT[memory_layers.WORKING]})[/dim]"
        )
        self.console.print(
            f"[dim]  Долговременная: "
            f"{plural_ru(len(report.long_term), 'запись', 'записи', 'записей')} "
            f"({memory_layers.LAYER_LIFETIME[memory_layers.LONG_TERM]})[/dim]"
        )
        for record in report.long_term:
            self.console.print(f"[dim]    {record.category} · {record.key}: {record.value}[/dim]")
        self.console.print(
            f"[dim]    хранилище: {report.long_term_store} "
            f"({memory_layers.LAYER_STORE_HINT[memory_layers.LONG_TERM]})[/dim]"
        )
        self.console.print("[dim]  Правила маршрутизации (что и куда попадает):[/dim]")
        for line in report.rules:
            self.console.print(f"[dim]    {line}[/dim]")
        self.console.print(
            "[dim]  Управление: /memory goal <цель> | /memory remember <сведение> | "
            "/memory forget <ключ|all>[/dim]"
        )

    def _handle_profile(self, user_input: str) -> None:
        """Команда /profile: без аргументов — отчёт, с аргументами — настройка или операция.

        Аргументы у команды значимы, как у `/memory`: подкоманда выбирает, что делать с профилем.
        Все операции профиля — методы агента, поэтому интерфейс не пишет в файл профилей сам.
        """
        parts = user_input.split(maxsplit=2)
        if len(parts) == 1:
            self._print_profile_report()
            return
        subcommand = parts[1]
        argument = parts[2].strip() if len(parts) > 2 else ""
        if subcommand == "setup":
            self._run_profile_interview()
            return
        if subcommand == "use" and argument:
            self._use_profile(argument)
            return
        if subcommand == "forget" and argument:
            self._forget_profile(argument)
            return
        self.console.print(f"[dim]{PROFILE_HINT}[/dim]")

    def _run_profile_interview(self) -> None:
        """Диалог настройки профиля: вопросы по одному, ответ — строкой с терминала.

        Вопросы печатает приложение, а ответы читаются `sys.stdin.readline` — как ручной ввод
        API-ключа: readline перехватывает SIGINT, поэтому с обычным `input()` Ctrl+C не поднял бы
        `KeyboardInterrupt` и отмена настройки не сработала бы. Профиль собирает агент, а строку о
        разделе интерфейс печатает по тому значению, которое уходит в файл.
        """
        state = user_profile.InterviewState()
        questions = plural_ru(state.total, "вопрос", "вопроса", "вопросов")
        self.console.print(
            f"[bold cyan]{PROFILE_INTERVIEW_HEADER.format(questions=questions)}[/bold cyan]"
        )
        try:
            while not state.finished:
                self.console.print(
                    f"[bold]Вопрос {state.index} из {state.total}. {state.question.prompt}[/bold]"
                )
                answer = self._read_profile_answer()
                state = state.answer(answer)
                value = user_profile.clip_value(answer)
                label = user_profile.FIELD_LABELS[state.last_field]
                self.console.print(
                    f"[dim]Профиль: {label} — {escape(value) or 'без изменений'}[/dim]"
                )
        except (KeyboardInterrupt, EOFError):
            self.console.print()
            self.console.print(f"[bold yellow]{PROFILE_INTERVIEW_CANCELLED}[/bold yellow]")
            return
        profile = self.agent.setup_profile(state)
        store = self.agent.profile_report().profile_store
        self.console.print(
            f"[bold green]Профиль «{escape(profile.name)}» сохранён и активен ({store}). "
            "Уходит системным сообщением в каждый запрос.[/bold green]"
        )

    def _read_profile_answer(self) -> str:
        """Читает ответ диалога настройки строкой без readline — как ручной ввод API-ключа."""
        self.console.print(PROFILE_ANSWER_PROMPT, end="")
        line = sys.stdin.readline()
        if line == "":
            raise EOFError
        return line.strip()

    def _use_profile(self, name: str) -> None:
        if self.agent.use_profile(name) is None:
            self.console.print(
                f"[dim]Профиля «{escape(name)}» нет — профиль заводит настройка: /profile setup.[/dim]"
            )
            return
        self.console.print(f"[dim]Профиль «{escape(name)}» теперь активен.[/dim]")

    def _forget_profile(self, name: str) -> None:
        if not self.agent.forget_profile(name):
            self.console.print(f"[dim]Профиля «{escape(name)}» нет.[/dim]")
            return
        self.console.print(f"[dim]Профиль «{escape(name)}» удалён.[/dim]")
        if not self.agent.profile_report().active_name:
            self.console.print(
                "[dim]Активного профиля нет — запросы уходят без персонализации.[/dim]"
            )

    def _print_profile_report(self) -> None:
        """Отчёт /profile: персонализация из снимка агента, без запросов к модели.

        Интерфейс не знает, как устроен файл профилей: он печатает снимок — активный профиль с
        заполненными разделами, имена всех профилей файла, путь файла и срок жизни профиля.
        """
        report = self.agent.profile_report()
        self.console.print("[bold cyan]Профиль пользователя (без обращения к модели):[/bold cyan]")
        if report.active_name:
            self.console.print(f"[dim]  Активный профиль: {escape(report.active_name)}[/dim]")
            for label, value in report.sections:
                self.console.print(f"[dim]    {label}: {escape(value)}[/dim]")
        else:
            self.console.print(
                "[dim]  Активного профиля нет — настройте его командой /profile setup.[/dim]"
            )
        names = escape(", ".join(report.names)) if report.names else "нет"
        self.console.print(f"[dim]  Профили файла: {names}[/dim]")
        self.console.print(
            f"[dim]    хранилище: {report.profile_store} "
            "(живёт между сессиями, /clear его не трогает)[/dim]"
        )
        self.console.print(
            "[dim]  В запрос: непустой профиль уходит системным сообщением в каждый вопрос.[/dim]"
        )
        self.console.print(f"[dim]  {PROFILE_HINT}[/dim]")

    # --- задача: команда, отчёт и прогон ---

    def _handle_task(self, user_input: str) -> None:
        """Команда /task: без аргументов — отчёт, с подкомандой — действие над очередью.

        Аргументы значимы, как у `/memory` и `/profile`: задача — это не вопрос модели, а очередь
        работ, поэтому у неё есть постановка, запуск и снятие. Прогон ведёт агент; здесь только
        цикл «выполнить операцию, перерисовать панель, опросить клавишу паузы».
        """
        _, _, argument = user_input.partition(" ")
        subcommand, _, rest = argument.strip().partition(" ")
        if not subcommand:
            self._print_task_report()
            return
        if subcommand == "add":
            self._add_task(rest)
            return
        if subcommand == "run":
            self._run_task_pipeline()
            return
        if subcommand == "stop":
            self._stop_tasks()
            return
        self.console.print(f"[dim]{TASK_HINT}[/dim]")

    def _add_task(self, goal: str) -> None:
        if not self.agent.add_task(goal):
            self.console.print(f"[bold yellow]Цель не указана.[/bold yellow] {TASK_HINT}")
            return
        report = self.agent.task_report()
        self.console.print(
            f"[bold green]Задача в очереди "
            f"({len(report.queue)} из {len(report.queue)}):[/bold green] "
            f"{escape(report.queue[-1].goal)}"
        )

    def _stop_tasks(self) -> None:
        report = self.agent.task_report()
        if not report.queue:
            self.console.print("[dim]Очередь задач и так пуста.[/dim]")
            return
        self.agent.drop_tasks()
        self.console.print(
            "[bold green]Очередь задач снята.[/bold green] Новую задачу ставит /task add."
        )

    def _run_task_pipeline(self) -> None:
        """Прогон конвейера задачи: живая панель, шаги агента и пауза по клавише или Ctrl+C.

        Пауза опрашивается перед каждой операцией, поэтому идущий запрос доводится до конца, а
        состояние к этому моменту уже записано на диск. Панель (rich.Live со transient) живёт
        ровно на время операции и исчезает между ними: живая перерисовка затирала бы строки
        журнала — вопрос о правках, метрики и итог задачи. След прогона в журнале именно эти
        строки и составляет.
        """
        report = self.agent.task_report()
        if not report.queue or report.active_number == 0:
            self.console.print(f"[dim]{TASK_EMPTY_HINT}[/dim]")
            return
        if self.agent.tasks_paused:
            self.agent.resume_tasks()
            self.console.print("[dim]Пауза снята — продолжаю с сохранённого шага.[/dim]")

        paused = False
        try:
            with keyboard.raw_mode():
                # Панель живёт весь прогон, включая ожидание ответа пользователя: состояние
                # задачи должно быть видно и тогда, когда от пользователя ждут правок. Строки
                # журнала (метрики, итоги) печатаются выше панели — rich умеет печатать поверх
                # живой области, — а ввод идёт в саму панель, поэтому она ничего не перекрывает.
                with Live(console=self.console, refresh_per_second=8, transient=True) as live:
                    def draw(busy: str = "", answer: Optional[str] = None) -> None:
                        live.update(self._run_frame(busy, answer))

                    self._task_spend = []
                    self._run_input = ""
                    draw()
                    while True:
                        if self._collect_typed_keys(draw):
                            # Enter с непустой строкой: прогон встаёт на паузу, а строку
                            # обработает главный цикл — как обычный ввод пользователя.
                            self._pending_input = self._run_input.strip()
                            paused = True
                            break
                        before = self.agent.task_report()
                        label = (
                            f"{before.active_stage}, {before.current_step}"
                            if before.active_stage
                            else ""
                        )
                        step = self.agent.task_step(
                            lambda phase: draw(TASK_PHASE_LABELS.get(phase, ""))
                        )
                        if step is None:
                            break
                        if step.meta is not None:
                            self._task_spend.append((label, step.meta))
                        self._print_task_step(step)
                        if step.needs_edits:
                            self.agent.task_answer_edits(self._ask_plan_edits(draw))
                        draw()
        except KeyboardInterrupt:
            # Ctrl+C в cbreak-режиме приходит сигналом: пауза безопаснее выхода (работа уже на
            # диске), а выйти всегда можно из приглашения командой /exit.
            paused = True

        if paused:
            report = self.agent.task_report()
            self.agent.pause_tasks()
            self.console.print(
                "[bold yellow]"
                + TASK_PAUSE_NOTICE.format(stage=report.active_stage, step=report.current_step)
                + "[/bold yellow]"
            )
            return
        self.console.print(f"[bold green]{TASK_EMPTY_EPILOGUE}[/bold green]")

    def _run_frame(self, busy: str = "", answer: Optional[str] = None):
        """Кадр прогона: панель, статус-бар под ней и поле ввода — как в главном цикле."""
        return Group(
            self._render_task_panel(self.agent.task_report(), busy, answer),
            Rule(style="dim"),
            Text.from_markup(f"[dim]{self._status_bar_line()}[/dim]"),
            Text.from_markup(f"{escape(INPUT_PROMPT)}{escape(self._run_input)}"),
        )

    def _collect_typed_keys(self, draw) -> bool:
        """Забирает набранное с терминала в поле ввода; True — Enter с непустой строкой.

        Клавиша паузы работает, только когда строка пуста: иначе набор «p» в начале строки
        останавливал бы прогон вместо того, чтобы попасть в ввод.
        """
        while True:
            key = keyboard.read_key_nowait()
            if not key:
                return False
            if key == TASK_PAUSE_KEY and not self._run_input:
                return True
            if key == keyboard.ENTER:
                if self._run_input.strip():
                    return True
                continue
            if key == keyboard.BACKSPACE:
                self._run_input = self._run_input[:-1]
            elif len(key) == 1:
                self._run_input += key
            draw()

    def _ask_plan_edits(self, draw) -> str:
        """Читает правки к плану по клавишам, отражая ввод в самой панели.

        `readline` здесь не годится: прогон идёт в cbreak-режиме (он нужен, чтобы клавиша паузы
        приходила сразу), а этот режим не отражает ввод — набранное было бы не видно, и панель
        пришлось бы закрывать на время вопроса, а она должна показывать состояние всегда. Поэтому
        строку собираем сами и показываем её прямо в панели; Ctrl+C во время ввода остаётся
        паузой (сигнал приходит в цикл прогона).
        """
        typed = ""
        draw(answer=typed)
        while True:
            key = keyboard.read_char()
            if key == keyboard.ENTER:
                break
            if key == keyboard.BACKSPACE:
                typed = typed[:-1]
            elif len(key) == 1:
                typed += key
            draw(answer=typed)
        notice = f"Правки: {typed.strip()}" if typed.strip() else "Правок нет — план принят."
        self.console.print(f"[dim]{escape(notice)}[/dim]")
        return typed

    def _print_task_step(self, step) -> None:
        """Строки журнала по итогам операции прогона: замечания и итог задачи.

        Строк метрик здесь намеренно нет: во время выполнения задачи траты показывает панель
        (последний запрос и итог по прогону), а журнал остаётся чистым — по просьбе пользователя.
        """
        if step.notice:
            self.console.print(f"[dim]{escape(step.notice)}[/dim]")
        if step.finished_task:
            self.console.print(f"[bold green]{self._task_summary(step)}[/bold green]")
        if step.result_path:
            self.console.print(f"[dim]Результат: {escape(step.result_path)}[/dim]")

    @staticmethod
    def _task_summary(step) -> str:
        """Строка итога задачи: «Итог: …» — общий маркер для всех трёх исходов.

        Факты приходят снимком шага, а фраза собирается здесь: русские склонения — дело
        интерфейса, и в отчётах приложения они собираются одним и тем же `plural_ru`.
        """
        sections = plural_ru(step.sections, "раздел", "раздела", "разделов")
        head = f"Итог: «{escape(step.goal)}» — "
        if step.failure:
            return f"{head}задача не удалась: {escape(step.failure)}"
        if step.issues:
            unresolved = "; ".join(escape(issue) for issue in step.issues)
            return (
                f"{head}задача завершена с замечаниями: {sections} артефакта, "
                f"{plural_ru(step.artifact_chars, 'символ', 'символа', 'символов')}; не закрыто: {unresolved}"
            )
        return (
            f"{head}задача решена: {sections} артефакта, {plural_ru(step.artifact_chars, 'символ', 'символа', 'символов')}, "
            "замечаний проверки нет"
        )

    def _print_task_report(self) -> None:
        """Отчёт /task: этапы, очередь, план с отметками и артефакт из снимка агента.

        Ни одного обращения к модели и ни одного чтения файла состояния: интерфейс рендерит
        снимок, правила автомата остаются в агенте.
        """
        report = self.agent.task_report()
        self.console.print("[bold]Задача агента[/bold]")
        stages = " → ".join(
            f"[reverse bold]{label}[/reverse bold]" if label == report.active_stage else label
            for label in report.stages
        )
        self.console.print(f"Этапы: {stages}")
        self.console.print()
        if not report.queue:
            self.console.print(f"[dim]{TASK_EMPTY_HINT}[/dim]")
            self.console.print(f"[dim]Состояние: {report.task_store}[/dim]")
            return
        self.console.print(f"Очередь ({len(report.queue)}):")
        for number, entry in enumerate(report.queue, start=1):
            marker = "▸" if number == report.active_number else " "
            if number == report.active_number:
                place = f"{entry.stage}, {entry.step}"
            else:
                place = (
                    f"{entry.status}, артефакт {plural_ru(entry.artifact_chars, 'символ', 'символа', 'символов')}"
                    if entry.artifact_chars
                    else entry.status
                )
            self.console.print(
                f"  {marker} {number}. {escape(entry.goal)} — {escape(place)}"
            )
        self.console.print()
        if report.active_number == 0:
            # Все задачи очереди завершены: активной нет, и полей этапа у отчёта тоже нет.
            self.console.print("Незавершённых задач нет — прогон остановлен.")
            self.console.print(f"  Состояние: {report.task_store}")
            self.console.print(f"[dim]{TASK_HINT}[/dim]")
            return
        active = report.queue[report.active_number - 1]
        self.console.print(f"Текущая задача: «{escape(active.goal)}»")
        self.console.print(
            f"  Этап: {report.active_stage} ({report.stages.index(report.active_stage) + 1} "
            f"из {len(report.stages)})"
        )
        self.console.print(f"  Текущий шаг: {escape(report.current_step)}")
        self.console.print(f"  Ожидаемое действие: {escape(report.expected_action)}")
        if report.plan:
            self.console.print("  План:")
            for item, mark in report.plan:
                self.console.print(
                    f"    {escape(TASK_MARK_LABELS.get(mark, mark))} {escape(item)}"
                )
        self.console.print(
            f"  Артефакт: {plural_ru(len(report.artifact_sections), 'раздел', 'раздела', 'разделов')}, "
            f"{plural_ru(report.artifact_chars, 'символ', 'символа', 'символов')}"
        )
        issues = "; ".join(escape(issue) for issue in report.issues) if report.issues else "—"
        self.console.print(f"  Замечания проверки: {issues}")
        self.console.print(f"  Пауза: {'да' if report.paused else 'нет'}")
        self.console.print(f"  Состояние: {report.task_store}")
        self.console.print(f"[dim]{TASK_HINT}[/dim]")

    def _render_task_panel(
        self, report: TaskReport, busy: str = "", answer: Optional[str] = None
    ) -> Panel:
        """Живая панель прогона: задача, этапы, шаг, действие, план списком и строка ввода.

        Панель показывается весь прогон, в том числе когда от пользователя ждут ответа: тогда
        вместо подписи фазы в ней строка вопроса с набранным текстом. План печатается по одной
        подзадаче на строку — следить за прогрессом по списку видно, а в одну строку нет.
        """
        active_index = (
            report.stages.index(report.active_stage) if report.active_stage in report.stages else 0
        )
        stages = []
        for index, label in enumerate(report.stages):
            if index == active_index:
                stages.append(f"[reverse bold]▸ {label}[/reverse bold]")
            else:
                stages.append(f"{'✓' if index < active_index else '·'} {label}")
        lines = [
            "Этапы:              " + "   ".join(stages),
            f"Текущий шаг:        {escape(report.current_step)}",
            f"Ожидаемое действие: {escape(report.expected_action)}",
        ]
        if report.plan:
            lines.append("План:")
            lines.extend(
                f"  {escape(TASK_MARK_LABELS.get(mark, mark))} {escape(item)}"
                for item, mark in report.plan
            )
        if report.artifact_chars:
            lines.append(
                f"Артефакт:           {plural_ru(len(report.artifact_sections), 'раздел', 'раздела', 'разделов')}, "
                f"{plural_ru(report.artifact_chars, 'символ', 'символа', 'символов')}"
            )
        if self._task_spend:
            label, meta = self._task_spend[-1]
            lines.append(f"Последний запрос:   {escape(label)} — {self._meta_summary(meta)}")
            lines.append(f"За прогон:          {self._spend_summary()}")
        # Как поставить задачу на паузу, панель говорит всегда — и во время запроса, и когда
        # ждёт правок, и когда ждёт клавишу.
        lines.append(f"[dim]{TASK_PAUSE_HINT}[/dim]")
        if busy:
            lines.append(f"[bold yellow]{busy}[/bold yellow]")
        if answer is not None:
            lines.append(
                "[bold]Правки к плану (Enter — принять, Ctrl+C — пауза):[/bold] "
                f"{escape(answer)}"
            )
        title = (
            f"Задача {report.active_number}/{len(report.queue)}: "
            f"«{escape(report.queue[report.active_number - 1].goal)}»"
        )
        return Panel("\n".join(lines), title=title, border_style="cyan", title_align="left")

    @staticmethod
    def _meta_summary(meta: AnswerMeta) -> str:
        cost = f"${meta.cost_usd:.4f}" if meta.cost_usd is not None else "неизвестно"
        return f"{meta.elapsed_seconds:.1f}с, {meta.total_tokens} ток., {cost}"

    def _spend_summary(self) -> str:
        """Траты прогона целиком: сколько запросов, токенов и денег он стоил."""
        tokens = sum(meta.total_tokens for _, meta in self._task_spend)
        if any(meta.cost_usd is None for _, meta in self._task_spend):
            cost = "неизвестно"
        else:
            cost = f"${sum(meta.cost_usd for _, meta in self._task_spend):.4f}"
        return f"{plural_ru(len(self._task_spend), 'запрос', 'запроса', 'запросов')}, {tokens} ток., {cost}"

    def _print_memory_line(self) -> None:
        """Строка о решении маршрута после ответа: какие слои получили запись из реплики.

        Печатается, только когда правило сработало, — иначе журнал шумел бы на каждом вопросе.
        В history.json строка не попадает, как и строки о сжатии и фактах.
        """
        records = self.agent.last_routing
        if not records:
            return
        parts = []
        for record in records:
            label = memory_layers.LAYER_LABELS[record.layer]
            if record.layer == memory_layers.LONG_TERM:
                parts.append(f"{label} ({record.category}: {record.key})")
            else:
                parts.append(f"{label} ({record.key})")
        self.console.print(f"[dim]Память: {', '.join(parts)}[/dim]")

    def _print_usage_meta(self, meta: AnswerMeta, label: str = "") -> None:
        cost = f"${meta.cost_usd:.6f}" if meta.cost_usd is not None else "неизвестно"
        where = f"{escape(label)}  |  " if label else ""
        self.console.print(
            f"[dim]⏱ {where}{meta.elapsed_seconds:.2f}с  |  "
            f"Токены: {meta.prompt_tokens}+{meta.completion_tokens}={meta.total_tokens}  |  "
            f"Стоимость: {cost}[/dim]"
        )

    def _print_compression_line(self) -> None:
        """Строка о сжатии после ответа/ошибки: печатается, когда сжатие выполнилось.

        Маркер — сам отчёт last_compression: TUI помнит напечатанный объект и печатает
        строку только когда агент сворачивал что-то заново. В history.json не попадает.
        """
        report = self.agent.last_compression
        if report is None or report is self._printed_compression:
            return
        self._printed_compression = report
        self.console.print(
            f"[dim]Контекст сжат: {report.messages} сообщений "
            f"({report.exchanges} обменов) → резюме[/dim]"
        )

    def _print_invariants_lines(self) -> bool:
        """Строки о нарушении инвариантов последним ответом — и только о нарушении.

        Соблюдённые инварианты не журналируются: строка на каждый ответ засоряла бы журнал, а
        статус-бар их не упоминает. Возвращает, отклонён ли ответ. В history.json строки не
        попадают.
        """
        check = self.agent.last_invariants
        if check is None or not check.violations:
            return False
        first = ", ".join(
            f"Инвариант {v.number} нарушен («{escape(v.term)}»)" for v in check.violations
        )
        self.console.print(f"[bold red]⛔ {first} — повторный запрос.[/bold red]")
        if check.rejected:
            final = "; ".join(
                f"инвариант {v.number} «{escape(v.rule)}» (в ответе: «{escape(v.term)}»)"
                for v in check.final_violations
            )
            self.console.print(f"[bold red]⛔ Ответ отклонён: {final}.[/bold red]")
        return check.rejected

    def _print_invariants_report(self) -> None:
        """Отчёт /invariants: таблица инвариантов из снимка агента, без запросов к модели.

        Аргументов у команды нет: инварианты фиксированы, команд управления ими не существует.
        """
        report = self.agent.invariants_report()
        self.console.print("[bold cyan]Инварианты агента (без обращения к модели):[/bold cyan]")
        for inv in report.invariants:
            self.console.print(f"[dim]  {inv.number}. {escape(inv.rule)}[/dim]")
            if inv.forbidden:
                words = ", ".join(escape(term) for term in inv.forbidden)
                self.console.print(f"[dim]     проверка кодом по словам: {words}[/dim]")
            else:
                self.console.print("[dim]     проверка: только модель (слов для кода нет)[/dim]")
        self.console.print(
            "[dim]  В запрос: уходят системным сообщением в каждый вопрос и в каждый запрос "
            "конвейера /task, сразу после профиля.[/dim]"
        )
        self.console.print(
            "[dim]  Проверка ответа: нарушивший ответ возвращается модели с перечнем нарушений "
            f"({config.INVARIANT_RETRIES} раз), затем отклоняется и заменяется отказом.[/dim]"
        )
        self.console.print(
            "[dim]  Отказ модели по инварианту начинается со слов «Не могу предложить» и "
            "называет инвариант.[/dim]"
        )

    def _print_facts_line(self) -> None:
        """Строка о блоке фактов: печатается один раз на изменение отчёта агента.

        Маркер — сам отчёт last_facts: TUI помнит напечатанный объект. Сбой извлекателя
        виден пользователю, но ответ на вопрос уже напечатан — блок остался прежним.
        Строки о фактах в history.json не попадают.
        """
        report = self.agent.last_facts
        if report is None or report is self._printed_facts:
            return
        self._printed_facts = report
        if report.updated:
            self.console.print(f"[dim]Факты обновлены: {report.keys} ключей.[/dim]")
        else:
            self.console.print("[dim]Факты не обновлены.[/dim]")

    def _print_typing(self, answer: str) -> None:
        with Live(console=self.console, refresh_per_second=30) as live:
            for end in range(TYPING_CHUNK_SIZE, len(answer) + TYPING_CHUNK_SIZE, TYPING_CHUNK_SIZE):
                live.update(Markdown(answer[:end]))
                time.sleep(TYPING_DELAY)
            live.update(Markdown(answer))

    def _status_bar_line(self) -> str:
        """Текст статус-бара одной строкой: его печатают и обычные шаги, и живая панель прогона."""
        commands_hint = ", ".join(STATUS_COMMANDS)
        session = self.agent.session_usage
        session_cost = f"${session.cost_usd:.4f}" if session.cost_usd is not None else "неизвестно"
        # Профиль печатается, только когда он непуст: пустой профиль в запрос не уходит, и строка
        # о нём говорила бы о персонализации, которой нет. Раскладка статус-бара без профиля
        # поэтому остаётся прежней.
        profile = self.agent.profile_report()
        profile_part = f"Профиль: {escape(profile.active_name)}  |  " if profile.sections else ""
        # Строка задачи — тоже только когда очередь непуста: пустая очередь в запрос не уходит,
        # и раскладка статус-бара без задач остаётся прежней.
        task = self.agent.task_report()
        task_part = ""
        if task.active_number:
            place = ", пауза" if task.paused else ""
            task_part = (
                f"Задача: {task.active_number}/{len(task.queue)} "
                f"«{escape(task.queue[task.active_number - 1].goal)}» — "
                f"{task.active_stage} {escape(task.current_step)}{place}  |  "
            )
        return (
            f"Статус: Готов ✅  |  Модель: {self.model}  |  {profile_part}{task_part}"
            f"Формат: {FORMAT_LABELS[self.settings.format]}  |  "
            f"Стратегия: {STRATEGY_LABELS[self.settings.context_strategy]}  |  "
            f"Объём: {self.settings.max_words} слов  |  Лимит списка: {self.settings.list_limit}  |  "
            f"Температура: {self.settings.temperature:.1f}  |  "
            f"Команды: {commands_hint}  |  "
            f"Сессия: {session.total_tokens} ток., {session_cost}"
        )

    def _print_status_bar(self) -> None:
        self.console.print(f"[dim]{self._status_bar_line()}[/dim]")
        self.console.rule(style="dim")

    def _exit(self) -> None:
        self.console.print(f"[bold yellow]{GOODBYE_MESSAGE}[/bold yellow]")
