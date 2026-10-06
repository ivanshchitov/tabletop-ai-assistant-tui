#!/usr/bin/env python3

"""Точка входа Tabletop AI Assistant."""

import os
import sys
import signal
from pathlib import Path

# Запуск «как есть» (./tabletop-ai-assistant.py) идёт через `env python3`, а это интерпретатор
# системы: зависимостей проекта там нет, и с них падает первый же импорт. Если рядом лежит
# окружение проекта, перезапускаем себя его интерпретатором — тогда запуск работает одинаково
# и из ./, и из `python3`, и из IDE, без активации окружения руками.
# Пакет mcp требует Python 3.10+, поэтому вернуться к системному 3.9 нельзя.
#
# Признак «мы уже внутри окружения» — sys.prefix, а не путь исполняемого файла: `.venv/bin/python`
# сам по себе симлинк на базовый интерпретатор, поэтому сравнение разрешённых путей считало любой
# запуск базовым интерпретатором запуском изнутри окружения и перезапуск не срабатывал.
_VENV_DIR = Path(__file__).resolve().parent / ".venv"
_VENV_PYTHON = _VENV_DIR / "bin" / "python"
if _VENV_PYTHON.exists() and Path(sys.prefix) != _VENV_DIR:
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

from ui.tui_app import TabletopAITUI  # noqa: E402  (импорт после возможного перезапуска)
from core import config
from core.api_client import APIClient
from core.llama_server import LlamaServer


def main() -> None:
    # Локальной модели ключ не нужен; облачная проверит его при запросе.
    app = TabletopAITUI(client=APIClient(config.get_api_key() or ""))
    server = LlamaServer()
    previous_handlers = {}

    def terminate(signum, frame):
        raise SystemExit(128 + signum)

    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous_handlers[sig] = signal.signal(sig, terminate)
        if os.getenv("TABLETOP_LLAMA_AUTOSTART", "1") != "0":
            app.console.print("[cyan]Запуск локального llama-server на 127.0.0.1:9999...[/cyan]")
        try:
            server.start()
        except (RuntimeError, OSError) as exc:
            from rich.markup import escape
            app.console.print(f"[yellow]Локальный сервер недоступен: {escape(str(exc))}[/yellow]")
        else:
            if os.getenv("TABLETOP_LLAMA_AUTOSTART", "1") != "0":
                app.console.print("[green]Локальный llama-server готов.[/green]")
        app.run()
    finally:
        try:
            server.stop()
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nВыход. Локальный сервер остановлен.")
