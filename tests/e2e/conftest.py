"""Фикстуры e2e-слоя: stub-сервер, фабрика сессий, снапшоты."""

import sys
import warnings
from pathlib import Path

import pytest

from .harness import AppSession
from .stub_api import StubAPI

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


@pytest.fixture
def tui_display(request):
    """Режим показа вывода приложения (`--show-tui`) плюс сам транслятор потока.

    В режиме `live` байты из псевдотерминала пишутся прямо в stdout процесса тестов, поэтому
    экран приложения выглядит так же, как при обычном запуске — со всеми перерисовками
    `rich.Live`. Писать нужно именно в дескриптор: приложение шлёт готовые управляющие
    последовательности, а не текст.
    """
    mode = request.config.getoption("--show-tui")
    if mode != "off" and request.config.getoption("capture") != "no":
        warnings.warn(
            "--show-tui без -s не покажет ничего: pytest перехватывает вывод. "
            "Запускайте как `pytest -s --show-tui=live ...`",
            stacklevel=2,
        )

    def mirror(data: bytes) -> None:
        stream = sys.__stdout__
        try:
            stream.buffer.write(data)
            stream.buffer.flush()
        except (ValueError, OSError):  # поток закрыт — показывать больше некуда
            pass

    return mode, (mirror if mode == "live" else None)


def _dump_screens(mode: str, sessions, title: str) -> None:
    """Печатает итоговый экран каждой сессии — режим `--show-tui=screen`."""
    if mode != "screen":
        return
    for index, session in enumerate(sessions, start=1):
        header = f" {title} — сессия {index} " if len(sessions) > 1 else f" {title} "
        print("\n" + header.center(100, "="))
        print(session.scrollback())
        print("=" * 100)


@pytest.fixture(autouse=True)
def _requires_unix():
    if sys.platform.startswith("win"):
        pytest.skip("e2e-прогон требует псевдотерминала Unix")


@pytest.fixture
def stub() -> StubAPI:
    api = StubAPI()
    api.url = api.start()
    try:
        yield api
    finally:
        api.stop()


@pytest.fixture
def history_file(tmp_path: Path) -> Path:
    return tmp_path / "history.json"


@pytest.fixture
def app(stub, history_file, tui_display, request):
    """Фабрика сессий: одна и та же история переживает несколько запусков подряд."""
    mode, mirror = tui_display
    sessions = []

    def factory(**kwargs) -> AppSession:
        kwargs.setdefault("mirror", mirror)
        session = AppSession(api_url=stub.url, history_file=history_file, **kwargs)
        sessions.append(session)
        return session

    try:
        yield factory
    finally:
        _dump_screens(mode, sessions, request.node.name)
        for session in sessions:
            session.close()


@pytest.fixture
def live_app(history_file, tui_display, request):
    """Сессия против настоящего OpenCode Zen — без stub-сервера и с реальным ключом.

    Пропускает тест, если ключа нет: репозиторий должен оставаться проверяемым без него.
    Приложение читает ключ из `.env` само, поэтому переменная окружения не задаётся.
    """
    from core import config

    if not config.get_api_key():
        pytest.skip("нужен настоящий OPENCODE_API_KEY в окружении или .env")

    mode, mirror = tui_display
    sessions = []

    def factory(**kwargs):
        kwargs.setdefault("api_key", None)  # ключ берётся приложением из .env
        kwargs.setdefault("mirror", mirror)
        session = AppSession(history_file=history_file, api_url=None, **kwargs)
        sessions.append(session)
        return session

    try:
        yield factory
    finally:
        _dump_screens(mode, sessions, request.node.name)
        for session in sessions:
            session.close()
