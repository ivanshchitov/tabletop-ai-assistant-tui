"""Хранилище планировщика: задания, журнал их прогонов и накопленные ими данные.

Файл общий для двух процессов, но без гонок по построению: пишет только серверный процесс — внутри
вызова инструмента планировщика, — а приложение файл лишь читает и перечитывает (`reload`). Формат —
конверт JSON, как у истории и памяти, с терпимым чтением: отсутствующий, пустой или повреждённый
файл читается как пустой планировщик, потому что потеря расписания не повод ронять сессию.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config


@dataclass(frozen=True)
class ScheduledJob:
    """Задание: какой инструмент сервера вызывать, с чем, как часто и когда в следующий раз."""

    number: int
    tool: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    every_minutes: int = config.MIN_EVERY_MINUTES
    next_run: float = 0.0
    runs: int = 0

    def is_due(self, now: float) -> bool:
        return self.next_run <= now


@dataclass(frozen=True)
class JobRun:
    """Прогон задания: когда, чем кончился, что собрал."""

    number: int
    at: float
    ok: bool
    summary: str
    collected: int = 0
    fresh: int = 0


def _read_jobs(raw: object) -> List[ScheduledJob]:
    if not isinstance(raw, list):
        return []
    jobs: List[ScheduledJob] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("tool"), str):
            continue
        arguments = item.get("arguments")
        jobs.append(
            ScheduledJob(
                number=int(item.get("number", len(jobs) + 1)),
                tool=item["tool"],
                arguments=dict(arguments) if isinstance(arguments, dict) else {},
                every_minutes=int(item.get("every_minutes", config.MIN_EVERY_MINUTES)),
                next_run=float(item.get("next_run", 0.0)),
                runs=int(item.get("runs", 0)),
            )
        )
    return jobs


def _read_runs(raw: object) -> List[JobRun]:
    if not isinstance(raw, list):
        return []
    runs: List[JobRun] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        runs.append(
            JobRun(
                number=int(item.get("number", 0)),
                at=float(item.get("at", 0.0)),
                ok=bool(item.get("ok", True)),
                summary=str(item.get("summary", "")),
                collected=int(item.get("collected", 0)),
                fresh=int(item.get("fresh", 0)),
            )
        )
    return runs


def _read_collected(raw: object) -> Dict[str, List[str]]:
    if not isinstance(raw, dict):
        return {}
    collected: Dict[str, List[str]] = {}
    for key, value in raw.items():
        if isinstance(value, list):
            collected[str(key)] = [str(name) for name in value]
    return collected


class ScheduleStore:
    """Файл планировщика: чтение при создании, запись сразу после изменения."""

    def __init__(self, path: Path = config.SCHEDULE_FILE):
        self.path = path
        self._jobs: List[ScheduledJob] = []
        self._runs: List[JobRun] = []
        self._collected: Dict[str, List[str]] = {}
        self.reload()

    def reload(self) -> None:
        """Перечитывает файл: приложение так видит прогоны, записанные серверным процессом."""
        data = self._load()
        self._jobs = _read_jobs(data.get("jobs"))
        self._runs = _read_runs(data.get("runs"))
        self._collected = _read_collected(data.get("collected"))

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def jobs(self) -> Tuple[ScheduledJob, ...]:
        return tuple(self._jobs)

    def job(self, number: int) -> Optional[ScheduledJob]:
        for item in self._jobs:
            if item.number == number:
                return item
        return None

    def runs(self) -> Tuple[JobRun, ...]:
        return tuple(self._runs)

    def due_jobs(self, now: float) -> Tuple[ScheduledJob, ...]:
        return tuple(item for item in self._jobs if item.is_due(now))

    def add_job(
        self, tool: str, arguments: Dict[str, Any], every_minutes: int, next_run: float
    ) -> ScheduledJob:
        """Ставит задание и отдаёт его с присвоенным номером; номера не переиспользуются."""
        number = max((item.number for item in self._jobs), default=0) + 1
        job = ScheduledJob(
            number=number,
            tool=tool,
            arguments=dict(arguments),
            every_minutes=every_minutes,
            next_run=next_run,
        )
        self._jobs.append(job)
        self.save()
        return job

    def record_run(
        self, job: ScheduledJob, at: float, ok: bool, summary: str, collected: int = 0, fresh: int = 0
    ) -> JobRun:
        """Записывает прогон и переносит срок задания на период вперёд — и после отказа тоже.

        Отказ сдвигает срок намеренно: иначе упавшее задание повторялось бы каждым тиком
        исполнителя, добивая недоступный внешний API вместо ожидания следующего периода.
        """
        run = JobRun(
            number=job.number, at=at, ok=ok, summary=summary, collected=collected, fresh=fresh
        )
        self._runs.append(run)
        del self._runs[: max(0, len(self._runs) - config.MAX_RUN_LOG)]
        self._jobs = [
            ScheduledJob(
                number=item.number,
                tool=item.tool,
                arguments=item.arguments,
                every_minutes=item.every_minutes,
                next_run=at + item.every_minutes * 60,
                runs=item.runs + 1,
            )
            if item.number == job.number
            else item
            for item in self._jobs
        ]
        self.save()
        return run

    def collected(self, key: str) -> Tuple[str, ...]:
        return tuple(self._collected.get(key, ()))

    def collected_total(self) -> int:
        return sum(len(names) for names in self._collected.values())

    def remember_collected(self, key: str, names: Iterable[str]) -> Tuple[str, ...]:
        """Накапливает собранные записи и отдаёт те из них, что встречены впервые."""
        known = self._collected.setdefault(key, [])
        fresh = [name for name in names if name not in known]
        known.extend(fresh)
        self.save()
        return tuple(fresh)

    def save(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "jobs": [
                            {
                                "number": job.number,
                                "tool": job.tool,
                                "arguments": job.arguments,
                                "every_minutes": job.every_minutes,
                                "next_run": job.next_run,
                                "runs": job.runs,
                            }
                            for job in self._jobs
                        ],
                        "runs": [
                            {
                                "number": run.number,
                                "at": run.at,
                                "ok": run.ok,
                                "summary": run.summary,
                                "collected": run.collected,
                                "fresh": run.fresh,
                            }
                            for run in self._runs
                        ],
                        "collected": self._collected,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError:
            pass
