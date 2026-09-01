"""TUI-приложение Tabletop AI Assistant на базе rich."""

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

from core import config, prompts
from core.answer_settings import AnswerFormat, AnswerSettings, AnswerSettingsError
from core.api_client import DeepseekAPIClient, DeepseekAPIError, is_valid_json_answer
from core.history_manager import HistoryManager

from . import keyboard

APP_TITLE = "🎲 TABLETOP AI ASSISTANT — эксперт по настольным играм"
WELCOME_MESSAGE = (
    "🎲 Tabletop AI Assistant запущен. Задайте вопрос по настольным играм. "
    "/settings — настройки формата и объёма ответа, /clear — очистить историю, /exit — выход."
)
GOODBYE_MESSAGE = "До встречи! История диалога сохранена. 🎲"
EXIT_BEFORE_START_MESSAGE = "До встречи! 🎲"
TYPING_CHUNK_SIZE = 3
TYPING_DELAY = 0.015
COMMANDS = ["/exit", "/settings", "/clear"]
SETTINGS_ROW_FORMAT = 0
SETTINGS_ROW_MAX_WORDS = 1
SETTINGS_ROW_LIST_LIMIT = 2
SETTINGS_ROWS_COUNT = 3

FORMAT_LABELS = {
    AnswerFormat.COMPACT: "компактный",
    AnswerFormat.JSON: "JSON",
    AnswerFormat.FREE: "свободный",
}


class TabletopAITUI:
    def __init__(self) -> None:
        self.console = Console()
        self.history = HistoryManager()
        self.session_count = 0
        self.client: Optional[DeepseekAPIClient] = None
        self.last_error: Optional[str] = None
        self.settings = AnswerSettings()
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
        try:
            api_key = self._ensure_api_key()
        except KeyboardInterrupt:
            # Ctrl+C во время ручного ввода API-ключа тоже должен завершать
            # приложение аккуратно, а не необработанным traceback. Диалогов
            # ещё не было, поэтому сообщение про сохранённую историю не нужно.
            self.console.print()
            self.console.print(f"[bold yellow]{EXIT_BEFORE_START_MESSAGE}[/bold yellow]")
            return
        self.client = DeepseekAPIClient(api_key)

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

    def _ensure_api_key(self) -> str:
        api_key = config.get_api_key()
        while not api_key:
            self.console.print(
                "[bold red]API-ключ не найден. Добавьте OPENCODE_API_KEY в файл .env[/bold red]"
            )
            entered = input("Введите OPENCODE_API_KEY вручную: ").strip()
            if entered:
                config.set_api_key_runtime(entered)
                api_key = entered
        return api_key

    def _handle_command(self, user_input: str) -> bool:
        """Обрабатывает команды, кроме /exit. Возвращает True, если команда распознана."""
        command = user_input.split(maxsplit=1)[0]
        if command == "/settings":
            self._open_settings_screen()
            return True
        if command == "/clear":
            self.history.clear()
            self.console.print("[bold green]История диалога очищена.[/bold green]")
            return True
        return False

    def _open_settings_screen(self) -> None:
        """Экран настроек: ↑/↓ — выбор поля, ←/→ — формат, цифры/Backspace — числовые поля, Esc — выход."""
        format_values = list(AnswerFormat)
        format_index = format_values.index(self.settings.format)
        max_words_input = str(self.settings.max_words)
        list_limit_input = str(self.settings.list_limit)
        row = SETTINGS_ROW_FORMAT

        def render() -> Panel:
            return self._render_settings_panel(row, format_values, format_index, max_words_input, list_limit_input)

        with Live(console=self.console, refresh_per_second=30, transient=True) as live, keyboard.raw_mode():
            live.update(render())
            while True:
                key = keyboard.read_key()
                if key == keyboard.ESC:
                    break
                elif key in (keyboard.UP, keyboard.DOWN):
                    row = (row + 1) % SETTINGS_ROWS_COUNT
                elif row == SETTINGS_ROW_FORMAT and key in (keyboard.LEFT, keyboard.RIGHT):
                    step = -1 if key == keyboard.LEFT else 1
                    format_index = (format_index + step) % len(format_values)
                    self.settings = self.settings.with_format(format_values[format_index])
                elif row == SETTINGS_ROW_MAX_WORDS and key == keyboard.BACKSPACE:
                    max_words_input = max_words_input[:-1]
                elif row == SETTINGS_ROW_MAX_WORDS and key.isdigit():
                    max_words_input += key
                elif row == SETTINGS_ROW_LIST_LIMIT and key == keyboard.BACKSPACE:
                    list_limit_input = list_limit_input[:-1]
                elif row == SETTINGS_ROW_LIST_LIMIT and key.isdigit():
                    list_limit_input += key
                live.update(render())

        if max_words_input.isdigit():
            try:
                self.settings = self.settings.with_max_words(int(max_words_input))
            except AnswerSettingsError as exc:
                self.console.print(f"[bold red]{exc}[/bold red]")
        else:
            self.console.print("[bold red]Максимальный объём ответа: введите число слов.[/bold red]")

        if list_limit_input.isdigit():
            try:
                self.settings = self.settings.with_list_limit(int(list_limit_input))
            except AnswerSettingsError as exc:
                self.console.print(f"[bold red]{exc}[/bold red]")
        else:
            self.console.print("[bold red]Лимит вариантов в списке: введите число.[/bold red]")

    def _render_settings_panel(
        self,
        row: int,
        format_values: List[AnswerFormat],
        format_index: int,
        max_words_input: str,
        list_limit_input: str,
    ) -> Panel:
        format_line = "   ".join(
            f"[reverse bold]{FORMAT_LABELS[f]}[/reverse bold]" if i == format_index else FORMAT_LABELS[f]
            for i, f in enumerate(format_values)
        )
        marker_format = "➤" if row == SETTINGS_ROW_FORMAT else " "
        marker_words = "➤" if row == SETTINGS_ROW_MAX_WORDS else " "
        marker_list_limit = "➤" if row == SETTINGS_ROW_LIST_LIMIT else " "
        words_display = (
            f"[reverse bold]{max_words_input or ' '}[/reverse bold]"
            if row == SETTINGS_ROW_MAX_WORDS
            else max_words_input
        )
        list_limit_display = (
            f"[reverse bold]{list_limit_input or ' '}[/reverse bold]"
            if row == SETTINGS_ROW_LIST_LIMIT
            else list_limit_input
        )
        body = (
            f"{marker_format} Формат ответа: {format_line}\n"
            f"{marker_words} Макс. объём ({config.MIN_MAX_WORDS}..{config.MAX_MAX_WORDS} слов): {words_display}\n"
            f"{marker_list_limit} Лимит вариантов в списке ({config.MIN_LIST_LIMIT}..{config.MAX_LIST_LIMIT}): {list_limit_display}\n"
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
                answer = self.client.ask(
                    prompts.build_system_message(self.settings.format),
                    user_prompt,
                    max_tokens=config.max_tokens_for_words(self.settings.max_words),
                )
            except DeepseekAPIError as exc:
                self.last_error = str(exc)
                self.console.print(f"[bold red]{exc}[/bold red]")
                self.console.print("[bold red]Попробуйте повторить запрос.[/bold red]")
                self.console.rule(style="dim")
                return

        self.console.print("[bold magenta]Tabletop AI Assistant:[/bold magenta]")
        self._print_typing(answer)
        if self.settings.format == AnswerFormat.JSON and not is_valid_json_answer(answer):
            self.console.print("[bold yellow]⚠ Модель не вернула валидный JSON.[/bold yellow]")
        self.console.rule(style="dim")
        self.history.add(question, answer)
        self.session_count += 1

    def _print_typing(self, answer: str) -> None:
        with Live(console=self.console, refresh_per_second=30) as live:
            for end in range(TYPING_CHUNK_SIZE, len(answer) + TYPING_CHUNK_SIZE, TYPING_CHUNK_SIZE):
                live.update(Markdown(answer[:end]))
                time.sleep(TYPING_DELAY)
            live.update(Markdown(answer))

    def _print_status_bar(self) -> None:
        commands_hint = ", ".join(COMMANDS)
        self.console.print(
            f"[dim]Статус: Готов ✅  |  Формат: {FORMAT_LABELS[self.settings.format]}  |  "
            f"Объём: {self.settings.max_words} слов  |  Лимит списка: {self.settings.list_limit}  |  "
            f"Команды: {commands_hint}  |  Диалогов за сессию: {self.session_count}[/dim]"
        )
        self.console.rule(style="dim")

    def _exit(self) -> None:
        self.history.save()
        self.console.print(f"[bold yellow]{GOODBYE_MESSAGE}[/bold yellow]")
