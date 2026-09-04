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

from core import config, logictask, prompts
from core.answer_settings import AnswerFormat, AnswerSettings
from core.api_client import (
    API_KEY_CHARSET_ERROR,
    AnswerMeta,
    APIClient,
    APIError,
    is_valid_api_key,
    is_valid_json_answer,
)
from core.history_manager import HistoryManager

from . import commands_screen, keyboard, logictask_screen, models_screen, settings_screen
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
        self.history = history if history is not None else HistoryManager()
        self.session_count = 0
        self.client: Optional[APIClient] = client
        self.last_error: Optional[str] = None
        self.settings = AnswerSettings()
        self.model = config.DEFAULT_MODEL
        self._exit_requested = False
        self._setup_autocomplete()

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
            self.history.clear()
            self.console.print("[bold green]История диалога очищена.[/bold green]")
            return True
        if command == "/logictask":
            self._run_logictask()
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

    def _run_logictask(self) -> None:
        """Прогон фиксированной задачи выбранной стратегией промптинга.

        Экспериментальный режим: результаты не пишутся в историю и не считаются диалогами.
        Панель выбора работает по той же схеме, что экран `/settings`: логика клавиш — в
        редьюсере `logictask_screen`, здесь только raw_mode, Live и read-key/redraw.
        Ошибка API прерывает остаток прогона, но не сессию.
        """
        self.console.print("[bold cyan]Выберите стратегию решения логической задачи:[/bold cyan]")
        self.console.print(logictask.LOGIC_TASK)
        for number, title in logictask_screen.STRATEGY_OPTIONS:
            self.console.print(f"  {number}. {title}")
        state = logictask_screen.initial_state()
        with Live(console=self.console, refresh_per_second=30, transient=True) as live, keyboard.raw_mode():
            live.update(self._render_logictask_panel(state))
            while True:
                key = keyboard.read_key()
                if key == keyboard.ESC:
                    return
                if key == keyboard.ENTER:
                    break
                state = logictask_screen.apply_key(state, key)
                live.update(self._render_logictask_panel(state))

        number, title = state.selected
        self._run_strategy(number, title)

    def _render_logictask_panel(self, state: logictask_screen.LogictaskScreenState) -> Panel:
        lines = []
        for index, (number, title) in enumerate(logictask_screen.STRATEGY_OPTIONS):
            if index == state.selected_index:
                lines.append(f"➤ [reverse bold]{number}. {title}[/reverse bold]")
            else:
                lines.append(f"  {number}. {title}")
        body = "\n".join(lines) + "\n\n[dim]↑/↓ — выбор, Enter — решить, Esc — отмена[/dim]"
        return Panel(body, title="Логическая задача", style="cyan")

    def _run_strategy(self, number: int, title: str) -> None:
        self.last_error = None
        assert self.client is not None

        def _call(system: str, user: str) -> Optional[AnswerMeta]:
            # Потолок токенов фиксируется от объёма по умолчанию: настройки ответа сессии
            # (формат, объём, лимит списка) к прогонам /logictask не применяются.
            max_tokens = config.max_tokens_for_words(config.DEFAULT_MAX_WORDS)
            with self.console.status("[bold yellow]● Отправка...[/bold yellow]", spinner="dots"):
                try:
                    return self.client.ask_with_usage(system, user, max_tokens=max_tokens, model=self.model)
                except APIError as exc:
                    self.last_error = str(exc)
                    self.console.print(f"[bold red]{exc}[/bold red]")
                    self.console.print("[bold red]Попробуйте повторить запрос.[/bold red]")
                    return None

        self.console.print(f"[bold cyan]Стратегия {number}: {title}[/bold cyan]")
        if number == 1:
            meta = _call(*logictask.build_direct_prompts())
            if meta is None:
                return
            self._print_typing(meta.content)
            self._print_usage_meta(meta)
        elif number == 2:
            meta = _call(*logictask.build_stepwise_prompts())
            if meta is None:
                return
            self._print_typing(meta.content)
            self._print_usage_meta(meta)
        elif number == 3:
            composed_meta = _call(*logictask.build_prompt_compose_prompts())
            if composed_meta is None:
                return
            self.console.print("[dim]Составленный моделью промпт:[/dim]")
            self._print_typing(composed_meta.content)
            self._print_usage_meta(composed_meta)
            meta = _call(*logictask.build_solve_with_prompt_prompts(composed_meta.content))
            if meta is None:
                return
            self._print_typing(meta.content)
            self._print_usage_meta(meta)
        else:
            for role in logictask.EXPERT_ROLES:
                expert_meta = _call(*logictask.build_expert_prompts(role))
                if expert_meta is None:
                    return
                self.console.print(f"[dim]{role}[/dim]")
                self._print_typing(expert_meta.content)
                self._print_usage_meta(expert_meta)
        self.console.rule(style="dim")

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
        marker_format = "➤" if state.row == settings_screen.ROW_FORMAT else " "
        marker_words = "➤" if state.row == settings_screen.ROW_MAX_WORDS else " "
        marker_list_limit = "➤" if state.row == settings_screen.ROW_LIST_LIMIT else " "
        marker_temperature = "➤" if state.row == settings_screen.ROW_TEMPERATURE else " "
        words_display = (
            f"[reverse bold]{state.max_words_input or ' '}[/reverse bold]"
            if state.row == settings_screen.ROW_MAX_WORDS
            else state.max_words_input
        )
        list_limit_display = (
            f"[reverse bold]{state.list_limit_input or ' '}[/reverse bold]"
            if state.row == settings_screen.ROW_LIST_LIMIT
            else state.list_limit_input
        )
        temperature_display = (
            f"[reverse bold]{state.temperature_input or ' '}[/reverse bold]"
            if state.row == settings_screen.ROW_TEMPERATURE
            else state.temperature_input
        )
        body = (
            f"{marker_format} Формат ответа: {format_line}\n"
            f"{marker_words} Макс. объём ({config.MIN_MAX_WORDS}..{config.MAX_MAX_WORDS} слов): {words_display}\n"
            f"{marker_list_limit} Лимит вариантов в списке ({config.MIN_LIST_LIMIT}..{config.MAX_LIST_LIMIT}): {list_limit_display}\n"
            f"{marker_temperature} Температура ({config.MIN_TEMPERATURE}..{config.MAX_TEMPERATURE}): {temperature_display}\n"
            "\n"
            "[dim]↑/↓ — поле, ←/→ — формат, цифры/Backspace — числовые поля, Esc — выход и сохранение[/dim]"
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

        assert self.client is not None
        user_prompt = prompts.build_user_prompt(question, self.settings)
        with self.console.status("[bold yellow]● Отправка...[/bold yellow]", spinner="dots"):
            try:
                meta = self.client.ask_with_usage(
                    prompts.build_system_message(self.settings.format),
                    user_prompt,
                    max_tokens=config.max_tokens_for_words(self.settings.max_words),
                    temperature=self.settings.temperature,
                    model=self.model,
                )
            except APIError as exc:
                self.last_error = str(exc)
                self.console.print(f"[bold red]{exc}[/bold red]")
                self.console.print("[bold red]Попробуйте повторить запрос.[/bold red]")
                self.console.rule(style="dim")
                return

        answer = meta.content
        self.console.print("[bold magenta]Tabletop AI Assistant:[/bold magenta]")
        self._print_typing(answer)
        if self.settings.format == AnswerFormat.JSON and not is_valid_json_answer(answer):
            self.console.print("[bold yellow]⚠ Модель не вернула валидный JSON.[/bold yellow]")
        self._print_usage_meta(meta)
        self.console.rule(style="dim")
        self.history.add(question, answer)
        self.session_count += 1

    def _print_usage_meta(self, meta: AnswerMeta) -> None:
        cost = f"${meta.cost_usd:.6f}" if meta.cost_usd is not None else "неизвестно"
        self.console.print(
            f"[dim]⏱ {meta.elapsed_seconds:.2f}с  |  "
            f"Токены: {meta.prompt_tokens}+{meta.completion_tokens}={meta.total_tokens}  |  "
            f"Стоимость: {cost}[/dim]"
        )

    def _print_typing(self, answer: str) -> None:
        with Live(console=self.console, refresh_per_second=30) as live:
            for end in range(TYPING_CHUNK_SIZE, len(answer) + TYPING_CHUNK_SIZE, TYPING_CHUNK_SIZE):
                live.update(Markdown(answer[:end]))
                time.sleep(TYPING_DELAY)
            live.update(Markdown(answer))

    def _print_status_bar(self) -> None:
        commands_hint = ", ".join(STATUS_COMMANDS)
        self.console.print(
            f"[dim]Статус: Готов ✅  |  Модель: {self.model}  |  Формат: {FORMAT_LABELS[self.settings.format]}  |  "
            f"Объём: {self.settings.max_words} слов  |  Лимит списка: {self.settings.list_limit}  |  "
            f"Температура: {self.settings.temperature:.1f}  |  "
            f"Команды: {commands_hint}  |  Диалогов за сессию: {self.session_count}[/dim]"
        )
        self.console.rule(style="dim")

    def _exit(self) -> None:
        self.history.save()
        self.console.print(f"[bold yellow]{GOODBYE_MESSAGE}[/bold yellow]")
