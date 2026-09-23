"""Планировщик в живом приложении: отчёт /schedule, строка итога и объявление новых прогонов.

Задания сюда кладёт не приложение: их ставит фоновая часть (собственный сервер проекта), поэтому
тест пишет их прямо в подменённый файл планировщика — ровно так же, как это сделал бы серверный
процесс. Приложение в этих проверках только читает и печатает.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.schedule_store import ScheduleStore
from tests.dnd_api_stub import DndAPIStub

from . import harness
from .harness import REPO_ROOT
from .stub_api import answer

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

DAEMON = REPO_ROOT / "tabletop-scheduler.py"


def put_job(schedule_file: Path, section: str = "spells", every_minutes: int = 5) -> ScheduleStore:
    store = ScheduleStore(path=schedule_file)
    store.add_job(
        tool="dnd_digest", arguments={"section": section}, every_minutes=every_minutes, next_run=0.0
    )
    return store


def run_daemon(schedule_file: Path, api_url: str) -> subprocess.CompletedProcess:
    """Один проход фонового исполнителя — как если бы он работал всё это время в фоне."""
    env = dict(os.environ)
    env["TABLETOP_SCHEDULE_FILE"] = str(schedule_file)
    env["TABLETOP_DND_API_URL"] = api_url
    return subprocess.run(
        [sys.executable, str(DAEMON), "--once"],
        env=env,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def dnd_stub():
    server = DndAPIStub()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_schedule_report_is_empty_without_jobs(app, stub):
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/schedule")
        text = session.wait_for("заданий нет")

    assert "Планировщик" in text
    # Отчёт не ходит к модели.
    assert stub.call_count == 0


def test_schedule_report_shows_jobs_and_runs(app, stub, schedule_file):
    store = put_job(schedule_file)
    store.record_run(store.jobs()[0], at=1000.0, ok=True, summary="собрано 5, впервые: 3", collected=5, fresh=3)
    store.remember_collected("spells/2014", ["лечение", "щит", "огненный шар"])

    with app() as session:
        session.wait_for_prompt()
        session.send_line("/schedule")
        text = session.wait_for("dnd_digest")

    assert "каждые 5" in text
    assert "прогонов 1" in text
    assert "накоплено" in text
    assert stub.call_count == 0


def test_startup_line_reports_the_scheduler(app, schedule_file):
    store = put_job(schedule_file)
    store.record_run(store.jobs()[0], at=1000.0, ok=True, summary="собрано 5", collected=5, fresh=5)

    with app() as session:
        text = session.wait_for("Планировщик:")

    assert "1 задание" in text
    assert "1 прогон" in text


def test_daemon_run_is_announced_after_the_next_answer(app, stub, schedule_file, dnd_stub):
    """Прогон случился в чужом процессе — агент сам сообщает о нём после очередного ответа."""
    stub.sequence(answer("Первый ответ"), answer("Второй ответ"), answer("Третий ответ"))
    put_job(schedule_file)

    with app() as session:
        session.wait_for_prompt()
        session.send_line("Первый вопрос")
        harness.wait_for_answers(session, 1)

        result = run_daemon(schedule_file, dnd_stub.url)
        assert result.returncode == 0, result.stdout + result.stderr

        session.send_line("Второй вопрос")
        harness.wait_for_answers(session, 2)
        text = session.wait_for("🗓 Планировщик")

        assert "прогон" in text
        # Повторно о том же прогоне не сообщают.
        session.send_line("Третий вопрос")
        harness.wait_for_answers(session, 3)
        tail = session.scrollback().split("Третий вопрос")[-1]

    assert "🗓 Планировщик" not in tail


def test_schedule_lines_do_not_reach_the_history_file(app, stub, schedule_file, history_file, dnd_stub):
    stub.sequence(answer("Первый ответ"), answer("Второй ответ"))
    put_job(schedule_file)

    with app() as session:
        session.wait_for_prompt()
        session.send_line("Первый вопрос")
        harness.wait_for_answers(session, 1)
        run_daemon(schedule_file, dnd_stub.url)
        session.send_line("Второй вопрос")
        harness.wait_for_answers(session, 2)
        session.wait_for("🗓 Планировщик")
        session.send_line("/exit")
        session.wait_for("История диалога сохранена")

    saved = json.loads(history_file.read_text(encoding="utf-8"))
    assert all("Планировщик" not in record["answer"] for record in saved["dialogues"])
    assert all("Планировщик" not in record["question"] for record in saved["dialogues"])


def test_schedule_is_listed_in_the_commands_panel(app, stub):
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/commands")
        text = session.wait_on_screen("планировщик")

    assert "/schedule" in text


def test_repository_schedule_file_is_untouched(app, stub, schedule_file, dnd_stub):
    """Ни приложение, ни исполнитель не трогают реальный schedule.json репозитория."""
    real = REPO_ROOT / "schedule.json"
    before = real.read_text(encoding="utf-8") if real.exists() else None

    put_job(schedule_file)
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/schedule")
        session.wait_for("dnd_digest")
    run_daemon(schedule_file, dnd_stub.url)

    after = real.read_text(encoding="utf-8") if real.exists() else None
    assert after == before
