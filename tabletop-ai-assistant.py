#!/usr/bin/env python3

"""Точка входа Tabletop AI Assistant."""

import os
import sys
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


def main() -> None:
    app = TabletopAITUI()
    app.run()


if __name__ == "__main__":
    main()
