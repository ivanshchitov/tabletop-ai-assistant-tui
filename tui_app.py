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

import config
import prompts
from api_client import DeepseekAPIClient, DeepseekAPIError
from history_manager import HistoryManager

APP_TITLE = "🎲 TABLETOP AI ASSISTANT — эксперт по настольным играм"
WELCOME_MESSAGE = (
    "🎲 Tabletop AI Assistant запущен. Задайте вопрос по настольным играм. /exit — выход."
)
GOODBYE_MESSAGE = "До встречи! История диалога сохранена. 🎲"
EXIT_BEFORE_START_MESSAGE = "До встречи! 🎲"
TYPING_CHUNK_SIZE = 3
TYPING_DELAY = 0.015
COMMANDS = ["/exit"]


class TabletopAITUI:
    def __init__(self) -> None:
        self.console = Console()
        self.history = HistoryManager()
        self.session_count = 0
        self.client: Optional[DeepseekAPIClient] = None
        self.last_error: Optional[str] = None
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
        with self.console.status("[bold yellow]● Отправка...[/bold yellow]", spinner="dots"):
            try:
                answer = self.client.ask(prompts.build_system_message(), question)
            except DeepseekAPIError as exc:
                self.last_error = str(exc)
                self.console.print(f"[bold red]{exc}[/bold red]")
                self.console.print("[bold red]Попробуйте повторить запрос.[/bold red]")
                self.console.rule(style="dim")
                return

        self.console.print("[bold magenta]Tabletop AI Assistant:[/bold magenta]")
        self._print_typing(answer)
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
            f"[dim]Статус: Готов ✅  |  Команды: {commands_hint} — выход  |  "
            f"Диалогов за сессию: {self.session_count}[/dim]"
        )
        self.console.rule(style="dim")

    def _exit(self) -> None:
        self.history.save()
        self.console.print(f"[bold yellow]{GOODBYE_MESSAGE}[/bold yellow]")
