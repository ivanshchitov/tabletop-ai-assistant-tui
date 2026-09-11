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
from rich.panel import Panel

from core import config
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
from core.tabletop_agent import RequestPhase, TabletopAgent

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
                    user_input = input("> Введите вопрос (или /exit для выхода): ")
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
            self.console.print("[bold green]История диалога очищена.[/bold green]")
            return True
        if command == "/usage":
            self._print_usage_report()
            return True
        if command == "/context":
            self._print_context_report()
            return True
        if command == "/branches":
            self._open_branches_screen()
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
                self._print_compression_line()
                self._print_facts_line()
                self.console.print(f"[bold red]{exc}[/bold red]")
                self.console.print("[bold red]Попробуйте повторить запрос.[/bold red]")
                self.console.rule(style="dim")
                return

        self._print_compression_line()
        self._print_facts_line()
        answer = meta.content
        self.console.print("[bold magenta]Tabletop AI Assistant:[/bold magenta]")
        self._print_typing(answer)
        if self.settings.format == AnswerFormat.JSON and not is_valid_json_answer(answer):
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

    def _print_usage_meta(self, meta: AnswerMeta) -> None:
        cost = f"${meta.cost_usd:.6f}" if meta.cost_usd is not None else "неизвестно"
        self.console.print(
            f"[dim]⏱ {meta.elapsed_seconds:.2f}с  |  "
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

    def _print_status_bar(self) -> None:
        commands_hint = ", ".join(STATUS_COMMANDS)
        session = self.agent.session_usage
        session_cost = f"${session.cost_usd:.4f}" if session.cost_usd is not None else "неизвестно"
        self.console.print(
            f"[dim]Статус: Готов ✅  |  Модель: {self.model}  |  Формат: {FORMAT_LABELS[self.settings.format]}  |  "
            f"Стратегия: {STRATEGY_LABELS[self.settings.context_strategy]}  |  "
            f"Объём: {self.settings.max_words} слов  |  Лимит списка: {self.settings.list_limit}  |  "
            f"Температура: {self.settings.temperature:.1f}  |  "
            f"Команды: {commands_hint}  |  "
            f"Сессия: {session.total_tokens} ток., {session_cost}[/dim]"
        )
        self.console.rule(style="dim")


    def _exit(self) -> None:
        self.console.print(f"[bold yellow]{GOODBYE_MESSAGE}[/bold yellow]")
