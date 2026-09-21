"""Прямой запуск точки входа: ./tabletop-ai-assistant.py работает любым интерпретатором.

Шебанг ведёт на `env python3` — интерпретатор системы, где зависимостей проекта нет. Точка
входа перезапускает себя интерпретатором окружения проекта, если оно лежит рядом; без этого
прямой запуск падал на первом же импорте.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .harness import ENTRY_POINT, REPO_ROOT

pytestmark = pytest.mark.e2e


def _isolated_env(tmp_path) -> dict:
    """Окружение запуска: файлы состояния — во временном каталоге, как и в остальных e2e."""
    env = dict(os.environ)
    env["TABLETOP_HISTORY_FILE"] = str(tmp_path / "history.json")
    env["TABLETOP_MEMORY_FILE"] = str(tmp_path / "memory.json")
    env["TABLETOP_PROFILE_FILE"] = str(tmp_path / "profile.json")
    env["TABLETOP_TASK_FILE"] = str(tmp_path / "task.json")
    env["TABLETOP_TASKS_DIR"] = str(tmp_path / "tasks")
    env["OPENCODE_API_KEY"] = "sk-entry-point-test"
    return env


def test_entry_point_is_executable_with_a_shebang():
    assert os.access(ENTRY_POINT, os.X_OK)
    assert ENTRY_POINT.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3")


@pytest.mark.skipif(
    not (REPO_ROOT / ".venv" / "bin" / "python").exists(),
    reason="перезапуск проверяется только при наличии окружения проекта рядом",
)
def test_entry_point_reexecutes_into_the_project_environment(tmp_path):
    """Запуск чужим интерпретатором доходит до приложения, а не падает на импорте.

    Запускать надо именно *базовым* интерпретатором окружения, а не sys.executable прогона:
    тесты и так идут внутри окружения, где перезапуск не нужен, и такой прогон проходил бы при
    сломанной проверке. Ровно этот случай и ломался: `.venv/bin/python` — симлинк на базовый
    интерпретатор, поэтому сравнение разрешённых путей считало базовый запуск запуском изнутри
    окружения.
    """
    base_python = Path(sys.base_prefix) / "bin" / "python3"
    if not base_python.exists() or base_python.resolve() == Path(sys.prefix).resolve():
        pytest.skip("базовый интерпретатор окружения недоступен отдельно")

    env = _isolated_env(tmp_path)

    result = subprocess.run(
        [str(base_python), str(ENTRY_POINT)],
        input="/exit\n",
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "ModuleNotFoundError" not in result.stderr
    assert "История диалога сохранена" in result.stdout


def test_entry_point_started_inside_the_environment_does_not_loop(tmp_path):
    """Запуск интерпретатором окружения не перезапускает себя по кругу."""
    env = _isolated_env(tmp_path)

    result = subprocess.run(
        [sys.executable, str(ENTRY_POINT)],
        input="/exit\n",
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "История диалога сохранена" in result.stdout
