#!/usr/bin/env python3

"""Фоновый исполнитель расписания Tabletop AI Assistant.

Отдельный процесс, потому что иначе «круглосуточно» не получается: MCP-клиент проекта одноразовый
(запуск процесса, рукопожатие, вызов, закрытие), поэтому цикл расписания не может жить внутри
сервера, а поток внутри приложения работал бы только при открытом приложении.

Исполнитель — обычный MCP-клиент: он зовёт у собственного сервера проекта выполнение просроченных
заданий, то есть проверяет тот же путь, которым идут все прочие вызовы. Результаты попадают в файл
планировщика, откуда их читает приложение.

Запуск: `./tabletop-scheduler.py` (тик раз в минуту) или `./tabletop-scheduler.py --once`
(выполнить просроченные один раз и выйти — этот режим гоняют тесты и демонстрация).
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Та же причина, что и в tabletop-ai-assistant.py: `env python3` — системный интерпретатор,
# где нет ни зависимостей проекта, ни пакета mcp.
_VENV_DIR = Path(__file__).resolve().parent / ".venv"
_VENV_PYTHON = _VENV_DIR / "bin" / "python"
if _VENV_PYTHON.exists() and Path(sys.prefix) != _VENV_DIR:
    os.execv(str(_VENV_PYTHON), [str(_VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

from core import config  # noqa: E402  (импорт после возможного перезапуска)
from core.mcp_client import MCPClient, MCPError  # noqa: E402

RUN_DUE_TOOL = "schedule_run_due"


def build_client(command: str = "") -> MCPClient:
    """Клиент собственного сервера проекта; `--command` подменяет команду запуска."""
    spec = config.MCP_SERVERS[config.SCHEDULER_SERVER]
    if command:
        spec = spec._replace(command=command)
    return MCPClient(spec)


def tick(client: MCPClient) -> bool:
    """Один проход расписания. False — проход не удался; цикл от этого не останавливается."""
    try:
        print(client.call_tool(RUN_DUE_TOOL, {}), flush=True)
        return True
    except MCPError as error:
        print(f"Планировщик: не удалось выполнить задания — {error}", flush=True)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Фоновый исполнитель расписания Tabletop AI Assistant")
    parser.add_argument(
        "--once", action="store_true", help="выполнить просроченные задания один раз и выйти"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=config.SCHEDULER_TICK_SECONDS,
        help=f"пауза между проходами в секундах (по умолчанию {config.SCHEDULER_TICK_SECONDS})",
    )
    parser.add_argument(
        "--command", default="", help="команда запуска сервера вместо записи реестра"
    )
    options = parser.parse_args()

    client = build_client(options.command)
    if options.once:
        tick(client)
        return

    print(
        f"Планировщик запущен: проход каждые {options.interval} с, файл {config.SCHEDULE_FILE}. "
        "Остановка — Ctrl+C.",
        flush=True,
    )
    try:
        while True:
            tick(client)
            time.sleep(options.interval)
    except KeyboardInterrupt:
        print("Планировщик остановлен.", flush=True)


if __name__ == "__main__":
    main()
