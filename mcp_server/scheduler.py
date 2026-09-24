"""Планировщик собственного MCP-сервера: отложенные и периодические вызовы его инструментов.

Модуль отделён от `dnd_tools.py` по той же причине, по которой объявление инструментов отделено
от протокола: здесь нет ни HTTP, ни асинхронности, исполнитель вызова инжектируется — значит вся
логика расписания (что просрочено, как сдвигается срок, как строится агрегат) проверяется обычным
юнит-тестом без запуска процесса и без сети.

Задание вызывает инструмент **этого же** сервера, внутри процесса: кросс-серверный вызов потребовал
бы второго MCP-клиента внутри сервера. Инструменты самого планировщика в задание не ставятся —
задание, зовущее `schedule_run_due`, зациклило бы исполнитель.

Отказ — это текст результата, а не исключение: его читает модель, которая и выбирала аргументы.
"""

import json
import time
from typing import Any, Callable, Dict, Sequence, Tuple

from core import config
from core.schedule_store import ScheduleStore

from .dnd_tools import ToolSpec

# Итог вызова, записываемый в журнал, обрезается: журнал уезжает в файл и в отчёт, а полный
# текст результата инструмента там не нужен — он в любом случае получен заново следующим вызовом.
SUMMARY_MAX_CHARS = 200

SCHEDULE_TOOLS: Tuple[ToolSpec, ...] = (
    ToolSpec(
        name="schedule_add",
        description=(
            "Поставить задание: вызывать инструмент этого сервера каждые N минут, при желании "
            "с отложенным первым запуском. Задание выполняется фоновым исполнителем расписания."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tool": {
                    "type": "string",
                    "description": "Имя инструмента этого сервера, который надо вызывать.",
                },
                "arguments": {
                    "type": "object",
                    "description": "Аргументы вызова инструмента, как при обычном вызове.",
                },
                "every_minutes": {
                    "type": "integer",
                    "description": (
                        f"Период повторения в минутах, от {config.MIN_EVERY_MINUTES} "
                        f"до {config.MAX_EVERY_MINUTES}."
                    ),
                },
                "start_in_minutes": {
                    "type": "integer",
                    "description": (
                        "Через сколько минут выполнить задание впервые, от "
                        f"{config.MIN_START_DELAY_MINUTES} до {config.MAX_START_DELAY_MINUTES}; "
                        "по умолчанию сразу."
                    ),
                    "default": config.MIN_START_DELAY_MINUTES,
                },
            },
            "required": ["tool", "every_minutes"],
        },
    ),
    ToolSpec(
        name="schedule_list",
        description="Перечень заданий планировщика: инструмент, период и время следующего запуска.",
        input_schema={"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        name="schedule_run_due",
        description=(
            "Выполнить задания, срок которых наступил, и записать их прогоны. Вызывается фоновым "
            "исполнителем расписания; задания, срок которых не наступил, не трогаются."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
    ),
    ToolSpec(
        name="schedule_summary",
        description=(
            "Агрегированный отчёт планировщика: задания с их расписанием, число прогонов, итог "
            "последнего прогона и объём накопленных данных."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
    ),
)

SCHEDULE_TOOL_NAMES = tuple(tool.name for tool in SCHEDULE_TOOLS)

ARGUMENT_ERROR_PREFIX = "Неверные аргументы"


class Scheduler:
    """Инструменты планировщика поверх хранилища и исполнителя вызова."""

    def __init__(
        self,
        store: ScheduleStore,
        call_tool: Callable[[str, Dict[str, Any]], Tuple[bool, str]],
        tool_names: Sequence[str],
        now: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self._call_tool = call_tool
        # Ставить можно только инструменты, не принадлежащие самому планировщику.
        self._tool_names = tuple(name for name in tool_names if name not in SCHEDULE_TOOL_NAMES)
        self._now = now

    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        return self.call_result(name, arguments)[1]

    def call_result(self, name: str, arguments: Dict[str, Any]) -> Tuple[bool, str]:
        """То же, но с признаком успеха: по нему сервер ставит протокольный признак ошибки."""
        handlers = {
            "schedule_add": self._add,
            "schedule_list": self._list,
            "schedule_run_due": self._run_due,
            "schedule_summary": self._report,
        }
        handler = handlers.get(name)
        if handler is None:
            return False, (
                f"Инструмент «{name}» не объявлен. Доступны: {', '.join(SCHEDULE_TOOL_NAMES)}."
            )
        text = handler(arguments or {})
        # Все отказы планировщика начинаются одной фразой: разбирать каждую ветку отдельно
        # значило бы переписать инструменты ради флага, которого прежде не требовалось.
        return not text.startswith(ARGUMENT_ERROR_PREFIX), text

    # --- инструменты ---

    def _add(self, arguments: Dict[str, Any]) -> str:
        tool = str(arguments.get("tool") or "").strip()
        if not tool:
            return "Неверные аргументы: параметр tool обязателен и не может быть пустым."
        if tool in SCHEDULE_TOOL_NAMES:
            return (
                f"Неверные аргументы: инструмент планировщика «{tool}» нельзя поставить в задание. "
                f"Доступны: {', '.join(self._tool_names)}."
            )
        if tool not in self._tool_names:
            return (
                f"Неверные аргументы: инструмент «{tool}» этот сервер не объявляет. "
                f"Доступны: {', '.join(self._tool_names)}."
            )

        every = self._whole(arguments, "every_minutes", config.MIN_EVERY_MINUTES, config.MAX_EVERY_MINUTES)
        if isinstance(every, str):
            return every
        delay = self._whole(
            arguments,
            "start_in_minutes",
            config.MIN_START_DELAY_MINUTES,
            config.MAX_START_DELAY_MINUTES,
            default=config.MIN_START_DELAY_MINUTES,
        )
        if isinstance(delay, str):
            return delay

        job_arguments = self._job_arguments(arguments.get("arguments"))
        if isinstance(job_arguments, str):
            return job_arguments
        job = self.store.add_job(
            tool=tool,
            arguments=job_arguments,
            every_minutes=every,
            next_run=self._now() + delay * 60,
        )
        when = "сразу" if delay == 0 else f"через {delay} мин"
        return (
            f"Задание {job.number} поставлено: инструмент {job.tool}, каждые {job.every_minutes} мин, "
            f"первый запуск {when}."
        )

    def _list(self, arguments: Dict[str, Any]) -> str:
        jobs = self.store.jobs()
        if not jobs:
            return "Планировщик: заданий нет."
        lines = ["Задания планировщика:"]
        for job in jobs:
            lines.append(
                f"- {job.number}: {job.tool} {self._arguments_text(job.arguments)}, "
                f"каждые {job.every_minutes} мин, следующий запуск {self._when(job.next_run)}, "
                f"прогонов {job.runs}"
            )
        return "\n".join(lines)

    def _run_due(self, arguments: Dict[str, Any]) -> str:
        now = self._now()
        due = self.store.due_jobs(now)
        if not due:
            return "Планировщик: просроченных заданий нет, выполнять нечего."
        lines = []
        for job in due:
            before = self.store.collected_total()
            ok, text = self._call_tool(job.tool, job.arguments)
            summary = self._summary(text)
            after = self.store.collected_total()
            self.store.record_run(
                job,
                at=now,
                ok=ok,
                summary=summary,
                collected=after,
                fresh=max(0, after - before),
            )
            mark = "выполнено" if ok else "отказ"
            lines.append(f"- {job.number} ({job.tool}): {mark} — {summary}")
        return "\n".join([f"Выполнено заданий: {len(due)}."] + lines)

    def _report(self, arguments: Dict[str, Any]) -> str:
        jobs = self.store.jobs()
        if not jobs:
            return "Планировщик: заданий нет, прогонов нет."
        runs = self.store.runs()
        lines = [
            f"Планировщик: заданий {len(jobs)}, прогонов {sum(job.runs for job in jobs)}, "
            f"накоплено записей {self.store.collected_total()}."
        ]
        for job in jobs:
            last = next((run for run in reversed(runs) if run.number == job.number), None)
            lines.append(
                f"- {job.number}: {job.tool} {self._arguments_text(job.arguments)}, "
                f"каждые {job.every_minutes} мин, следующий запуск {self._when(job.next_run)}, "
                f"прогонов {job.runs}"
            )
            if last is not None:
                mark = "выполнено" if last.ok else "отказ"
                lines.append(f"  последний прогон: {mark} — {last.summary}")
        return "\n".join(lines)

    # --- разбор аргументов и форматирование ---

    def _whole(
        self,
        arguments: Dict[str, Any],
        key: str,
        minimum: int,
        maximum: int,
        default: Any = None,
    ):
        """Целое в диапазоне — либо текст отказа, который уйдёт результатом вызова."""
        raw = arguments.get(key, default)
        if raw is None:
            return f"Неверные аргументы: параметр {key} обязателен."
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return f"Неверные аргументы: {key} должен быть целым числом, получено «{raw}»."
        if not minimum <= value <= maximum:
            return (
                f"Неверные аргументы: {key} должен быть от {minimum} до {maximum}, получено {value}."
            )
        return value

    def _job_arguments(self, raw: Any):
        """Аргументы задания: объект — как есть, строка — JSON (иначе текст отказа).

        Строкой они приходят двумя путями: из ручного `/tool call`, который разбирает ввод
        парами «ключ=значение», и от моделей, охотно присылающих вложенный объект строкой.
        """
        if isinstance(raw, dict):
            return dict(raw)
        if raw in (None, ""):
            return {}
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except ValueError:
                return (
                    "Неверные аргументы: arguments должен быть объектом или строкой JSON, "
                    f"получено «{raw}»."
                )
            if isinstance(parsed, dict):
                return parsed
        return f"Неверные аргументы: arguments должен быть объектом, получено «{raw}»."

    def _summary(self, text: str) -> str:
        first = (text or "").strip().splitlines()
        summary = first[0] if first else ""
        return summary[:SUMMARY_MAX_CHARS]

    def _arguments_text(self, arguments: Dict[str, Any]) -> str:
        if not arguments:
            return "без аргументов"
        return ", ".join(f"{key}={value}" for key, value in arguments.items())

    def _when(self, moment: float) -> str:
        return time.strftime("%H:%M:%S", time.localtime(moment))
