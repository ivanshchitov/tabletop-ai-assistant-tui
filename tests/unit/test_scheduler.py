"""Планировщик собственного MCP-сервера: постановка, просроченность, прогоны, агрегат.

Процесс сервера здесь не поднимается: исполнитель вызова инжектируется, поэтому вся логика
расписания проверяется без протокола и без сети — тот же приём, что у редьюсеров панелей.
"""

import pytest

from core import config
from core.schedule_store import ScheduleStore
from mcp_server.scheduler import SCHEDULE_TOOL_NAMES, Scheduler


class FakeClock:
    def __init__(self, value: float = 1000.0):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, minutes: float) -> None:
        self.value += minutes * 60


class FakeTools:
    """Исполнитель вызова: записывает обращения, отвечает заданным исходом."""

    def __init__(self, outcome=(True, "Сводка раздела «monsters»: собрано 2, впервые: 2")):
        self.calls = []
        self.outcome = outcome
        self.failures = set()

    def __call__(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name in self.failures:
            return False, "Источник данных не ответил: внешний API недоступен"
        return self.outcome


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def tools():
    return FakeTools()


@pytest.fixture
def scheduler(tmp_path, clock, tools):
    return Scheduler(
        store=ScheduleStore(path=tmp_path / "schedule.json"),
        call_tool=tools,
        tool_names=("dnd_digest", "dnd_search"),
        now=clock,
    )


# --- постановка задания ---


def test_added_job_is_listed_with_its_number(scheduler):
    text = scheduler.call("schedule_add", {"tool": "dnd_digest", "every_minutes": 5})
    assert "1" in text
    assert "dnd_digest" in scheduler.call("schedule_list", {})


def test_job_without_delay_is_due_at_once(scheduler, tools):
    scheduler.call("schedule_add", {"tool": "dnd_digest", "every_minutes": 5})
    scheduler.call("schedule_run_due", {})
    assert [name for name, _ in tools.calls] == ["dnd_digest"]


def test_delayed_job_waits_for_its_first_run(scheduler, tools, clock):
    scheduler.call("schedule_add", {"tool": "dnd_digest", "every_minutes": 5, "start_in_minutes": 10})
    assert "нечего" in scheduler.call("schedule_run_due", {})
    assert tools.calls == []

    clock.advance(10)
    scheduler.call("schedule_run_due", {})
    assert [name for name, _ in tools.calls] == ["dnd_digest"]


def test_arguments_of_the_job_reach_the_tool(scheduler, tools):
    scheduler.call(
        "schedule_add",
        {"tool": "dnd_digest", "arguments": {"section": "spells"}, "every_minutes": 5},
    )
    scheduler.call("schedule_run_due", {})
    assert tools.calls[0][1] == {"section": "spells"}


def test_unknown_tool_is_rejected_without_creating_a_job(scheduler):
    text = scheduler.call("schedule_add", {"tool": "dnd_dragons", "every_minutes": 5})
    assert "dnd_digest" in text
    assert "заданий нет" in scheduler.call("schedule_list", {})


def test_scheduler_tool_cannot_be_scheduled(scheduler):
    """Задание, зовущее сам планировщик, зациклило бы исполнитель — такой вызов отклоняется."""
    text = scheduler.call("schedule_add", {"tool": "schedule_run_due", "every_minutes": 5})
    assert "нельзя" in text.lower() or "недопустим" in text.lower()
    assert "заданий нет" in scheduler.call("schedule_list", {})


def test_period_out_of_range_is_rejected(scheduler):
    too_often = scheduler.call("schedule_add", {"tool": "dnd_digest", "every_minutes": 0})
    too_rare = scheduler.call(
        "schedule_add", {"tool": "dnd_digest", "every_minutes": config.MAX_EVERY_MINUTES + 1}
    )
    assert str(config.MAX_EVERY_MINUTES) in too_often
    assert str(config.MIN_EVERY_MINUTES) in too_rare
    assert "заданий нет" in scheduler.call("schedule_list", {})


def test_delay_out_of_range_is_rejected(scheduler):
    text = scheduler.call(
        "schedule_add",
        {
            "tool": "dnd_digest",
            "every_minutes": 5,
            "start_in_minutes": config.MAX_START_DELAY_MINUTES + 1,
        },
    )
    assert str(config.MAX_START_DELAY_MINUTES) in text
    assert "заданий нет" in scheduler.call("schedule_list", {})


def test_missing_tool_argument_is_rejected(scheduler):
    text = scheduler.call("schedule_add", {"every_minutes": 5})
    assert "tool" in text


# --- выполнение просроченных ---


def test_due_run_records_the_run_and_moves_the_next_run(scheduler, clock):
    scheduler.call("schedule_add", {"tool": "dnd_digest", "every_minutes": 5})
    scheduler.call("schedule_run_due", {})

    job = scheduler.store.jobs()[0]
    assert job.runs == 1
    assert job.next_run == clock.value + 5 * 60
    assert scheduler.store.runs()[0].ok is True


def test_nothing_is_due(scheduler, tools, clock):
    scheduler.call("schedule_add", {"tool": "dnd_digest", "every_minutes": 5})
    scheduler.call("schedule_run_due", {})
    tools.calls.clear()

    text = scheduler.call("schedule_run_due", {})
    assert "нечего" in text
    assert tools.calls == []


def test_failed_job_does_not_stop_the_others(scheduler, tools):
    scheduler.call("schedule_add", {"tool": "dnd_digest", "arguments": {"section": "spells"}, "every_minutes": 5})
    scheduler.call("schedule_add", {"tool": "dnd_search", "arguments": {"section": "monsters"}, "every_minutes": 5})
    tools.failures.add("dnd_digest")

    scheduler.call("schedule_run_due", {})

    assert [name for name, _ in tools.calls] == ["dnd_digest", "dnd_search"]
    runs = {run.number: run for run in scheduler.store.runs()}
    assert runs[1].ok is False
    assert "недоступен" in runs[1].summary
    assert runs[2].ok is True


def test_run_due_without_jobs_is_successful(scheduler):
    assert "нечего" in scheduler.call("schedule_run_due", {})


# --- агрегированный отчёт ---


def test_report_without_jobs(scheduler):
    assert "заданий нет" in scheduler.call("schedule_summary", {})


def test_report_names_schedule_runs_and_collected(scheduler, clock):
    scheduler.call(
        "schedule_add",
        {"tool": "dnd_digest", "arguments": {"section": "spells"}, "every_minutes": 5},
    )
    scheduler.store.remember_collected("spells/2014", ["лечение", "щит"])
    scheduler.call("schedule_run_due", {})

    text = scheduler.call("schedule_summary", {})
    assert "dnd_digest" in text
    assert "5" in text
    assert "накоплено" in text.lower()
    assert "2" in text


def test_report_does_not_call_tools(scheduler, tools):
    scheduler.call("schedule_add", {"tool": "dnd_digest", "every_minutes": 5})
    tools.calls.clear()
    scheduler.call("schedule_summary", {})
    scheduler.call("schedule_list", {})
    assert tools.calls == []


# --- объявление инструментов ---


def test_scheduler_declares_four_tools():
    assert set(SCHEDULE_TOOL_NAMES) == {
        "schedule_add",
        "schedule_list",
        "schedule_run_due",
        "schedule_summary",
    }


def test_unknown_scheduler_tool_is_reported(scheduler):
    assert "не объявлен" in scheduler.call("schedule_drop", {})
