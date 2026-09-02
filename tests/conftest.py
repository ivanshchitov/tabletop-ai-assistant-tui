"""Общие фикстуры для всех уровней тестов."""

import io
import sys
import time
from pathlib import Path
from typing import List

import pytest
from rich.console import Console

from core import config, prompts
from core.answer_settings import AnswerSettings
from core.history_manager import HistoryManager


@pytest.fixture(autouse=True)
def clear_prompt_caches():
    """Сбрасывает lru_cache в prompts до и после каждого теста.

    Без этого подмена config.ASSETS_DIR не влияет ни на что: базовый промпт и инструкции
    формата кэшируются навсегда при первом обращении и утекают между тестами.
    """
    prompts._get_base_system_prompt.cache_clear()
    prompts.get_format_instruction.cache_clear()
    prompts.build_system_message.cache_clear()
    yield
    prompts._get_base_system_prompt.cache_clear()
    prompts.get_format_instruction.cache_clear()
    prompts.build_system_message.cache_clear()


@pytest.fixture
def history_path(tmp_path: Path) -> Path:
    return tmp_path / "history.json"


@pytest.fixture
def history(history_path: Path) -> HistoryManager:
    return HistoryManager(path=history_path)


@pytest.fixture
def no_sleep(monkeypatch) -> List[float]:
    """Убирает реальные паузы и записывает запрошенные длительности.

    Экспоненциальный бэкофф в api_client иначе добавил бы к прогону несколько секунд, а сам
    список пауз — это ровно то, что нужно проверить (1, 2, 4 ...).
    """
    slept: List[float] = []

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(time, "sleep", fake_sleep)
    return slept


class RecordingConsole:
    """Console, пишущая в буфер, вместе с доступом к накопленному тексту."""

    def __init__(self, width: int = 100) -> None:
        self.buffer = io.StringIO()
        # force_terminal=False и no_color: нужен читаемый текст без ANSI, чтобы ассерты
        # смотрели на содержимое, а не на управляющие последовательности.
        self.console = Console(
            file=self.buffer,
            width=width,
            force_terminal=False,
            no_color=True,
            highlight=False,
            legacy_windows=False,
        )

    @property
    def text(self) -> str:
        return self.buffer.getvalue()

    def contains(self, needle: str) -> bool:
        # rich переносит длинные строки по ширине консоли, поэтому искомая подстрока может
        # оказаться разорванной переводом строки — ищем по тексту со схлопнутыми пробелами.
        return _collapse(needle) in _collapse(self.text)


def _collapse(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def recording_console() -> RecordingConsole:
    return RecordingConsole()


@pytest.fixture
def default_settings() -> AnswerSettings:
    return AnswerSettings()


@pytest.fixture
def real_assets_dir() -> Path:
    return config.ASSETS_DIR


@pytest.fixture
def skip_on_windows():
    if sys.platform.startswith("win"):
        pytest.skip("тест требует Unix-терминала")


def pytest_addoption(parser):
    parser.addoption(
        "--snapshot-update",
        action="store_true",
        default=False,
        help="перезаписать снапшоты экранов e2e вместо сравнения с ними",
    )
    parser.addoption(
        "--show-tui",
        action="store",
        default="off",
        choices=("off", "live", "screen"),
        help=(
            "показывать вывод приложения в терминале запуска тестов: "
            "live — транслировать поток по мере работы (выглядит как настоящий сеанс), "
            "screen — печатать итоговый экран после каждого теста. "
            "Требует -s, иначе pytest перехватит вывод."
        ),
    )
    parser.addoption(
        "--record-cassettes",
        action="store_true",
        default=False,
        help="перезаписать кассеты ответами живого OpenCode Zen (нужен ключ, тратит квоту)",
    )


@pytest.fixture
def snapshot_update(request) -> bool:
    return request.config.getoption("--snapshot-update")
