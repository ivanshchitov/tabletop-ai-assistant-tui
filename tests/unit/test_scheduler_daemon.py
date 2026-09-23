"""Фоновый исполнитель расписания как отдельный процесс.

Исполнитель — обычный MCP-клиент: он поднимает собственный сервер проекта и зовёт выполнение
просроченных заданий по протоколу. Прогон подменяет и внешний API (локальной заглушкой), и файл
планировщика, поэтому проверка не зависит ни от сети, ни от состояния репозитория.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.dnd_api_stub import DndAPIStub

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DAEMON = REPO_ROOT / "tabletop-scheduler.py"


@pytest.fixture
def stub():
    server = DndAPIStub()
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def environment(stub, tmp_path):
    env = dict(os.environ)
    env["TABLETOP_DND_API_URL"] = stub.url
    env["TABLETOP_SCHEDULE_FILE"] = str(tmp_path / "schedule.json")
    return env


def run_daemon(env, *args, timeout=60):
    return subprocess.run(
        [sys.executable, str(DAEMON), *args],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def schedule_file(env):
    return Path(env["TABLETOP_SCHEDULE_FILE"])


def add_job(env, section="monsters", every_minutes=5):
    """Ставит задание тем же путём, что и приложение, — вызовом инструмента сервера."""
    from core import config
    from core.mcp_client import MCPClient

    spec = config.MCP_SERVERS[config.SCHEDULER_SERVER]
    os.environ["TABLETOP_DND_API_URL"] = env["TABLETOP_DND_API_URL"]
    os.environ["TABLETOP_SCHEDULE_FILE"] = env["TABLETOP_SCHEDULE_FILE"]
    MCPClient(spec).call_tool(
        "schedule_add",
        {"tool": "dnd_digest", "arguments": {"section": section}, "every_minutes": every_minutes},
    )


def test_single_run_executes_the_due_job_and_exits(environment):
    add_job(environment)
    result = run_daemon(environment, "--once")

    assert result.returncode == 0
    data = json.loads(schedule_file(environment).read_text(encoding="utf-8"))
    assert data["runs"], result.stdout + result.stderr
    assert data["runs"][0]["ok"] is True
    assert data["collected"]["monsters/2014"]


def test_single_run_without_jobs_is_successful(environment):
    result = run_daemon(environment, "--once")
    assert result.returncode == 0
    assert "нечего" in result.stdout


def test_run_moves_the_next_run_so_a_second_tick_does_nothing(environment):
    add_job(environment)
    run_daemon(environment, "--once")
    first = json.loads(schedule_file(environment).read_text(encoding="utf-8"))

    run_daemon(environment, "--once")
    second = json.loads(schedule_file(environment).read_text(encoding="utf-8"))

    assert len(second["runs"]) == len(first["runs"]) == 1


def test_unavailable_server_is_reported_without_a_crash(environment, tmp_path):
    """Отказ сервера печатается и не роняет исполнитель: в цикле тик просто пропускается."""
    result = run_daemon(environment, "--once", "--command", str(tmp_path / "нет-такой-команды"))

    assert result.returncode == 0
    assert "не удалось" in (result.stdout + result.stderr).lower()
