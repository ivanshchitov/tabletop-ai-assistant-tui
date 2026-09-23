"""Хранилище планировщика: задания, журнал прогонов, накопленные данные."""

import json

from core import config
from core.schedule_store import ScheduleStore


def store(tmp_path):
    return ScheduleStore(path=tmp_path / "schedule.json")


def test_missing_file_reads_as_empty_scheduler(tmp_path):
    empty = store(tmp_path)
    assert empty.jobs() == ()
    assert empty.runs() == ()


def test_broken_file_reads_as_empty_scheduler(tmp_path):
    path = tmp_path / "schedule.json"
    path.write_text("{не json", encoding="utf-8")
    broken = ScheduleStore(path=path)
    assert broken.jobs() == ()
    assert broken.runs() == ()


def test_added_job_gets_number_and_survives_reload(tmp_path):
    path = tmp_path / "schedule.json"
    first = ScheduleStore(path=path)
    job = first.add_job(tool="dnd_digest", arguments={"section": "spells"}, every_minutes=5, next_run=100.0)
    assert job.number == 1
    assert job.runs == 0

    again = ScheduleStore(path=path)
    assert [item.tool for item in again.jobs()] == ["dnd_digest"]
    assert again.jobs()[0].arguments == {"section": "spells"}
    assert again.jobs()[0].every_minutes == 5


def test_numbers_do_not_repeat(tmp_path):
    scheduler = store(tmp_path)
    scheduler.add_job(tool="dnd_digest", arguments={}, every_minutes=5, next_run=0.0)
    second = scheduler.add_job(tool="dnd_digest", arguments={}, every_minutes=5, next_run=0.0)
    assert second.number == 2


def test_recorded_run_moves_the_next_run_and_counts(tmp_path):
    scheduler = store(tmp_path)
    job = scheduler.add_job(tool="dnd_digest", arguments={}, every_minutes=5, next_run=100.0)
    scheduler.record_run(job, at=100.0, ok=True, summary="собрано 3", collected=3, fresh=3)

    saved = scheduler.jobs()[0]
    assert saved.runs == 1
    assert saved.next_run == 100.0 + 5 * 60
    assert [run.summary for run in scheduler.runs()] == ["собрано 3"]
    assert scheduler.runs()[0].fresh == 3


def test_failed_run_is_recorded_too(tmp_path):
    scheduler = store(tmp_path)
    job = scheduler.add_job(tool="dnd_digest", arguments={}, every_minutes=5, next_run=100.0)
    scheduler.record_run(job, at=100.0, ok=False, summary="внешний API недоступен", collected=0, fresh=0)

    run = scheduler.runs()[0]
    assert run.ok is False
    assert "недоступен" in run.summary
    assert scheduler.jobs()[0].next_run == 100.0 + 5 * 60


def test_run_log_is_capped_but_collected_data_is_not(tmp_path):
    scheduler = store(tmp_path)
    job = scheduler.add_job(tool="dnd_digest", arguments={}, every_minutes=1, next_run=0.0)
    for index in range(config.MAX_RUN_LOG + 5):
        scheduler.record_run(job, at=float(index), ok=True, summary=f"прогон {index}", collected=1, fresh=0)
    scheduler.remember_collected("spells/2014", [f"запись-{index}" for index in range(config.MAX_RUN_LOG + 5)])

    assert len(scheduler.runs()) == config.MAX_RUN_LOG
    assert scheduler.runs()[-1].summary == f"прогон {config.MAX_RUN_LOG + 4}"
    assert len(scheduler.collected("spells/2014")) == config.MAX_RUN_LOG + 5


def test_collected_reports_only_names_seen_for_the_first_time(tmp_path):
    scheduler = store(tmp_path)
    fresh = scheduler.remember_collected("spells/2014", ["огненный шар", "лечение"])
    assert fresh == ("огненный шар", "лечение")

    repeated = scheduler.remember_collected("spells/2014", ["лечение", "щит"])
    assert repeated == ("щит",)
    assert scheduler.collected("spells/2014") == ("огненный шар", "лечение", "щит")


def test_collected_is_kept_per_key(tmp_path):
    scheduler = store(tmp_path)
    scheduler.remember_collected("spells/2014", ["лечение"])
    assert scheduler.remember_collected("monsters/2014", ["лечение"]) == ("лечение",)


def test_file_is_written_as_json_envelope(tmp_path):
    path = tmp_path / "schedule.json"
    scheduler = ScheduleStore(path=path)
    job = scheduler.add_job(tool="dnd_digest", arguments={"section": "spells"}, every_minutes=5, next_run=1.0)
    scheduler.record_run(job, at=1.0, ok=True, summary="итог", collected=1, fresh=1)
    scheduler.remember_collected("spells/2014", ["лечение"])

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"jobs", "runs", "collected"}
    assert data["jobs"][0]["tool"] == "dnd_digest"
    assert data["collected"]["spells/2014"] == ["лечение"]


def test_reload_picks_up_another_process_write(tmp_path):
    """Приложение читает тот же файл, что пишет серверный процесс: снимок обновляется перечитыванием."""
    path = tmp_path / "schedule.json"
    reader = ScheduleStore(path=path)
    writer = ScheduleStore(path=path)
    writer.add_job(tool="dnd_digest", arguments={}, every_minutes=5, next_run=0.0)

    assert reader.jobs() == ()
    reader.reload()
    assert len(reader.jobs()) == 1
