"""Запуск настоящего приложения в псевдотерминале с эмуляцией экрана.

Сырой поток из pty читать бесполезно: `rich.Live` перерисовывает кадр десятки раз в секунду
курсорными последовательностями, поэтому ответ модели окажется размазан по сотням фрагментов
вперемешку с управляющими кодами. Байты скармливаются в `pyte`, и тесты смотрят на
отрисованный экран — ровно то, что видит человек.

Ждать «тишины» в потоке тоже нельзя (по той же причине — кадры идут постоянно даже без
изменений), поэтому ожидание построено на опросе отрисованного экрана: `wait_for` ищет
подстроку, а `read_for` просто читает фиксированное время там, где проверяется отсутствие
чего-либо.
"""

import errno
import fcntl
import os
import pty
import re
import signal
import struct
import sys
import termios
import time
from pathlib import Path
from typing import Callable, List, Optional

import pyte

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENTRY_POINT = REPO_ROOT / "tabletop-ai-assistant.py"

PROMPT = "Введите вопрос"
DEFAULT_TIMEOUT = 10.0

# Клавиши как их присылает настоящий терминал.
KEY_UP = b"\x1b[A"
KEY_DOWN = b"\x1b[B"
KEY_RIGHT = b"\x1b[C"
KEY_LEFT = b"\x1b[D"
KEY_ESC = b"\x1b"
KEY_BACKSPACE = b"\x7f"
KEY_ENTER = b"\r"
KEY_TAB = b"\t"
CTRL_C = b"\x03"
CTRL_D = b"\x04"


def _collapse(text: str) -> str:
    return " ".join(text.split())


class AppSession:
    """Живой процесс приложения, подключённый к псевдотерминалу."""

    def __init__(
        self,
        history_file: Path,
        api_url: Optional[str] = None,
        cols: int = 100,
        rows: int = 40,
        api_key: Optional[str] = "sk-e2e-test",
        extra_env: Optional[dict] = None,
        mirror: Optional[Callable[[bytes], None]] = None,
    ) -> None:
        """Запускает приложение в псевдотерминале.

        `api_url=None` оставляет приложению его собственный адрес — то есть настоящий
        OpenCode Zen; `api_key=None` не задаёт ключ в окружении, и приложение читает его само
        из `.env`. Обе заглушки по умолчанию включены, живой режим включается явно.

        `mirror` получает каждый прочитанный из терминала кусок байт — через него вывод
        приложения транслируется в терминал, где запущены тесты (флаг `--show-tui`).
        """
        self._mirror = mirror
        self.cols = cols
        self.rows = rows
        # HistoryScreen хранит уехавшие вверх строки: главный цикл приложения append-only,
        # и прошлые обмены обязаны оставаться в скроллбэке.
        self.screen = pyte.HistoryScreen(cols, rows, history=2000, ratio=0.5)
        self.stream = pyte.ByteStream(self.screen)

        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "TERM": "xterm-256color",
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "COLUMNS": str(cols),
            "LINES": str(rows),
            "TABLETOP_HISTORY_FILE": str(history_file),
            # Анимация печати выключена всегда: она не влияет на итоговый текст, но растягивает
            # прогон. Таймаут сжимается только для stub-сервера — живой модели нужны десятки
            # секунд, поэтому там остаётся значение приложения по умолчанию.
            "TABLETOP_TYPING_DELAY": "0",
        }
        if api_key is not None:
            env["OPENCODE_API_KEY"] = api_key
        if api_url is not None:
            env["OPENCODE_API_URL"] = api_url
            env["TABLETOP_REQUEST_TIMEOUT"] = "1"
        env.update(extra_env or {})

        self.pid, self.fd = pty.fork()
        if self.pid == 0:  # дочерний процесс
            os.execve(sys.executable, [sys.executable, str(ENTRY_POINT)], env)
            os._exit(1)  # pragma: no cover

        self._set_window_size(cols, rows)
        os.set_blocking(self.fd, False)
        self._closed = False

    # --- служебное ---

    def _set_window_size(self, cols: int, rows: int) -> None:
        """Фиксированный размер окна: иначе rich перенесёт строки иначе и ассерты поплывут."""
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _drain(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):  # процесс закрыл терминал
                    self._closed = True
                    return
                raise
            if not data:
                self._closed = True
                return
            self.stream.feed(data)
            if self._mirror is not None:
                self._mirror(data)

    # --- чтение экрана ---

    def screen_text(self) -> str:
        self._drain()
        return "\n".join(self.screen.display)

    def scrollback(self) -> str:
        """Экран вместе с уехавшими вверх строками."""
        self._drain()
        top = [_render(line, self.cols) for line in self.screen.history.top]
        return "\n".join(top + list(self.screen.display))

    def read_for(self, seconds: float) -> str:
        """Читает фиксированное время. Для проверок «этого на экране быть не должно»."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._drain()
            time.sleep(0.02)
        return self.screen_text()

    def wait_for(self, needle: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """Ждёт появления подстроки в скроллбэке, опрашивая отрисованный экран."""
        target = _collapse(needle)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.scrollback()
            if target in _collapse(text):
                return text
            time.sleep(0.02)
        raise AssertionError(
            f"Не дождались {needle!r} за {timeout} с.\n--- экран ---\n{self.scrollback()}"
        )

    def wait_for_prompt(self, timeout: float = DEFAULT_TIMEOUT) -> str:
        return self.wait_for(PROMPT, timeout=timeout)

    def wait_on_screen(self, needle: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """Ждёт подстроку в текущем экране, игнорируя скроллбэк.

        Нужно там, где искомое уже встречалось выше по логу: поиск по скроллбэку в таком
        случае срабатывает мгновенно на старом вхождении и ничего не проверяет.
        """
        target = _collapse(needle)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.screen_text()
            if target in _collapse(text):
                return text
            time.sleep(0.02)
        raise AssertionError(
            f"Не дождались {needle!r} на экране за {timeout} с.\n--- экран ---\n{self.screen_text()}"
        )

    def wait_until_gone(self, needle: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """Ждёт, пока подстрока исчезнет с текущего экрана (например, закроется панель)."""
        target = _collapse(needle)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.screen_text()
            if target not in _collapse(text):
                return text
            time.sleep(0.02)
        raise AssertionError(
            f"{needle!r} не исчезло с экрана за {timeout} с.\n--- экран ---\n{self.screen_text()}"
        )

    def contains(self, needle: str) -> bool:
        return _collapse(needle) in _collapse(self.scrollback())

    # --- ввод ---

    def _write_all(self, data: bytes, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Дописывает буфер целиком.

        Один os.write в pty записывает лишь столько, сколько влезло в буфер терминала
        (обычно несколько килобайт). Длинный ввод без дозаписи потерял бы хвост вместе с
        завершающим Enter, и приложение просто ждало бы конца строки.
        """
        deadline = time.monotonic() + timeout
        while data:
            try:
                written = os.write(self.fd, data)
            except BlockingIOError:
                written = 0
            data = data[written:]
            if data:
                if time.monotonic() > deadline:
                    raise AssertionError("Не удалось дописать ввод в терминал за отведённое время")
                # Дать приложению вычитать накопившееся, освободив место в буфере.
                time.sleep(0.01)
                self._drain()

    def send_line(self, text: str) -> None:
        self._write_all(text.encode("utf-8") + b"\r")

    def send_key(self, key: bytes, count: int = 1) -> None:
        self._write_all(key * count)

    def send_keys(self, *keys: bytes) -> None:
        for key in keys:
            self._write_all(key)
            # Клавиши экрана настроек отправляются по одной с зазором: приложение читает их
            # по байту, а пачка escape-последовательностей в один write — отдельный сценарий.
            time.sleep(0.02)

    def ask(self, question: str, expect: str, timeout: float = DEFAULT_TIMEOUT) -> str:
        """Задать вопрос и дождаться ответа — самый частый шаг сценария."""
        self.wait_for_prompt()
        self.send_line(question)
        return self.wait_for(expect, timeout=timeout)

    # --- завершение ---

    def wait_exit(self, timeout: float = DEFAULT_TIMEOUT) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._drain()
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self._drain()
                return os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status
            time.sleep(0.02)
        raise AssertionError(
            f"Приложение не завершилось за {timeout} с.\n--- экран ---\n{self.scrollback()}"
        )

    def close(self) -> None:
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __enter__(self) -> "AppSession":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


def _render(line, cols: int) -> str:
    """Собирает строку скроллбэка pyte в текст."""
    return "".join(line[x].data for x in range(cols)).rstrip()


# Порт stub-сервера выбирается операционной системой заново на каждый прогон и попадает на
# экран внутри текста ошибки — в снапшоте он заменяется плейсхолдером.
_VOLATILE_PATTERNS = [(re.compile(r"127\.0\.0\.1:\d+"), "127.0.0.1:PORT")]


_MARKDOWN_CHARS = re.compile(r"[*_`#>]")


def plain_tail(markdown_text: str, words: int = 5) -> str:
    """Хвост ответа в том виде, в каком он окажется на экране.

    Искать по сырому markdown нельзя: rich отрисовывает `**7 Wonders**` жирным начертанием,
    и звёздочек в тексте экрана уже нет.
    """
    cleaned = _MARKDOWN_CHARS.sub("", markdown_text)
    return " ".join(cleaned.split()[-words:])


def normalize_screen(text: str) -> str:
    """Готовит экран к сравнению со снапшотом: гасит изменчивые фрагменты, убирает пустые края."""
    for pattern, replacement in _VOLATILE_PATTERNS:
        text = pattern.sub(replacement, text)
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def assert_snapshot(actual: str, path: Path, update: bool) -> None:
    """Сравнивает экран с сохранённым снапшотом (или обновляет его по флагу)."""
    actual = normalize_screen(actual)
    if update or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual + "\n", encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8").rstrip("\n")
    assert actual == expected, (
        f"Экран разошёлся со снапшотом {path.name}. "
        f"Обновить: pytest --snapshot-update\n"
        f"--- ожидалось ---\n{expected}\n--- получено ---\n{actual}"
    )
