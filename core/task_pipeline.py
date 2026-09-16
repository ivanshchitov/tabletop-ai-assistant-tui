"""Конвейер задачи: одна операция за вызов — запрос плана, подзадача, проверка или итог.

Модуль не знает ни про терминал, ни про HTTP: запрос к модели приходит инъекцией
(`ask(messages, max_words, phase) -> AnswerMeta`), поэтому каждая ветка конвейера проверяется
тестом с фальшивым запросом. Состояние живёт в `task_state`, конвейер применяет к нему переходы и
пишет файл до следующей операции.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import config, task_state
from .api_client import APIError, AnswerMeta
from .task_state import Stage, TaskIssue, TaskItem, TaskState, TaskStore

# Фазы запросов конвейера: агент превращает их в подписи индикатора.
PHASE_PLAN = "task_plan"
PHASE_EXECUTE = "task_execute"
PHASE_VALIDATE = "task_validate"

# Потолок объёма запроса на фазу: у плана и вердикта проверки он свой — им нужен запас под JSON,
# у раздела артефакта свой — он ограничен инструкцией объёма.
_PHASE_WORDS = {
    PHASE_PLAN: config.TASK_PLAN_MAX_WORDS,
    PHASE_EXECUTE: config.TASK_SECTION_MAX_WORDS + 30,
    PHASE_VALIDATE: config.TASK_VALIDATE_MAX_WORDS,
}

PLAN_ASSET = "task_plan_prompt.md"
EXECUTE_ASSET = "task_execute_prompt.md"
VALIDATE_ASSET = "task_validate_prompt.md"

_ASK_ATTEMPTS = 2
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

_EXECUTE_FAILED_EMPTY = "модель вернула пустой ответ"
_EXECUTE_FAILED_TRUNCATED = "ответ обрезан техническим потолком запроса"

AskCallable = Callable[[List[Dict[str, str]], int, str], AnswerMeta]


class TaskRequestFailed(Exception):
    """Запрос конвейера не дал пригодного ответа: повторять его бессмысленно."""


@dataclass(frozen=True)
class TaskStepReport:
    """Что сделал один шаг конвейера: куда пришла задача и что об этом сказать пользователю."""

    goal: str
    stage: Stage
    step: str
    expected_action: str
    plan: Tuple[str, ...] = ()
    needs_edits: bool = False
    finished_task: bool = False
    # Итог задачи отдаётся фактами: фразу собирает интерфейс — русские склонения («1 раздел»,
    # «2 раздела», «5 разделов») не дело ядра, а текст итога показывает терминальный слой.
    sections: int = 0
    artifact_chars: int = 0
    issues: Tuple[str, ...] = ()
    failure: str = ""
    notice: str = ""
    # Путь файла результата: задача записана в каталог tasks/, и интерфейс показывает, куда.
    result_path: str = ""
    meta: Optional[AnswerMeta] = None


def parse_plan_response(text: str) -> Tuple[str, ...]:
    """Разбирает ответ планирования: JSON-объект с пунктами плана или список строк.

    Неразобранный ответ — сбой операции, а не пустой план: пустой план молча оставил бы задачу
    без работы, тогда как сбой виден и повторяется.
    """
    data = _first_json(text)
    items: Sequence[object] = ()
    if isinstance(data, dict):
        raw = data.get("items", data.get("plan", []))
        if isinstance(raw, list):
            items = raw
    elif isinstance(data, list):
        items = data
    plan = tuple(
        item.strip()[: config.TASK_PLAN_ITEM_MAX_CHARS]
        for item in items
        if isinstance(item, str) and item.strip()
    )
    return plan[: config.MAX_PLAN_ITEMS]


def parse_validation_response(
    text: str,
) -> Optional[Tuple[bool, Tuple[TaskIssue, ...], Tuple[str, ...]]]:
    """Разбирает вердикт проверки: `{"ok", "issues", "items"}`; None — ответ не разобран.

    `items` — работа, которой в плане не было: проверка называет её, и она становится новой
    подзадачей. Так замечание «этого не хватает» закрывается делом, а не остаётся висеть.
    """
    data = _first_json(text)
    if not isinstance(data, dict) or "ok" not in data:
        return None
    issues: List[TaskIssue] = []
    raw_issues = data.get("issues")
    if isinstance(raw_issues, list):
        for entry in raw_issues:
            if isinstance(entry, str) and entry.strip():
                issues.append(TaskIssue(item=0, text=entry.strip()))
            elif isinstance(entry, dict):
                text_value = entry.get("issue") or entry.get("text") or ""
                if str(text_value).strip():
                    issues.append(TaskIssue(item=_as_int(entry.get("item")), text=str(text_value).strip()))
    items: List[str] = []
    raw_items = data.get("items")
    if isinstance(raw_items, list):
        items = [item.strip() for item in raw_items if isinstance(item, str) and item.strip()]
    if not bool(data.get("ok")) and not issues and not items:
        issues.append(TaskIssue(item=0, text="проверка не прошла, замечаний не названо"))
    return bool(data.get("ok")), tuple(issues), tuple(items)


def _first_json(text: str) -> Optional[object]:
    """Первый JSON-документ из ответа: модель может обернуть его в пояснения или блок кода."""
    match = _JSON_OBJECT_RE.search(text or "")
    candidate = match.group(0) if match else (text or "").strip()
    try:
        return json.loads(candidate)
    except (ValueError, TypeError):
        return None


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def deterministic_issues(task: TaskItem) -> Tuple[TaskIssue, ...]:
    """Проверки артефакта без модели: у каждой подзадачи плана есть непустой добротный раздел."""
    issues: List[TaskIssue] = []
    for index, item in enumerate(task.plan):
        if index >= len(task.sections):
            issues.append(TaskIssue(item=index + 1, text="подзадача не выполнена"))
            continue
        section = task.sections[index]
        if section.failed:
            issues.append(TaskIssue(item=index + 1, text=f"подзадача не выполнена: {section.failed}"))
        elif not section.text.strip():
            issues.append(TaskIssue(item=index + 1, text="пустой раздел артефакта"))
        elif section.truncated:
            issues.append(TaskIssue(item=index + 1, text=_EXECUTE_FAILED_TRUNCATED))
    return tuple(issues)


class TaskPipeline:
    """Шаговый исполнитель конвейера: одно действие и одна смена состояния за вызов."""

    def __init__(
        self,
        store: TaskStore,
        ask: AskCallable,
        results_dir: Path = config.TASK_RESULTS_DIR,
    ):
        self.store = store
        self._ask = ask
        self.results_dir = results_dir

    @property
    def state(self) -> TaskState:
        return self.store.state

    def answer_edits(self, text: str) -> TaskState:
        """Принимает ответ пользователя о правках и сохраняет переход."""
        state = task_state.edits_response(self.store.state, text)
        self.store.save(state)
        return state

    def step(self) -> Optional[TaskStepReport]:
        """Одна операция конвейера; None — незавершённых задач в очереди нет."""
        state = self.store.state
        if state.finished:
            return None
        task = state.active
        if task.stage is Stage.PLANNING:
            return self._planning(task, state)
        if task.stage is Stage.EXECUTION:
            return self._execution(task, state)
        return self._validation(task, state)

    # --- этапы ---

    def _planning(self, task: TaskItem, state: TaskState) -> TaskStepReport:
        if state.awaiting_edits:
            # Правки ждём без запроса: вопрос задаёт терминальный слой, ответ возвращается сюда.
            return self._report(task, state, needs_edits=True)

        if task.status is task_state.TaskStatus.PENDING:
            state = task_state.begin_task(state)
            self.store.save(state)
            task = state.active

        try:
            meta, plan = self._ask_plan(task)
        except TaskRequestFailed as exc:
            return self._fail(task, state, str(exc))

        notice = ""
        if task.replan:
            notice = f"правки приняты — план строится заново (круг {task.plan_round + 1})"
        state = task_state.plan_built(state, plan)
        self.store.save(state)
        return self._report(
            state.active, state, needs_edits=True, notice=notice, meta=meta
        )

    def _execution(self, task: TaskItem, state: TaskState) -> TaskStepReport:
        index = task_state.next_item_index(task)
        if index is None:
            # Все подзадачи выполнены: следующий шаг — проверка (отдельная операция, без запроса).
            state = task_state.start_validation(state)
            self.store.save(state)
            return self._report(state.active, state)

        try:
            meta = self._ask_execute(task, index)
        except TaskRequestFailed as exc:
            state = task_state.add_section(self.store.state, "", failed=str(exc))
            self.store.save(state)
            return self._report(
                state.active,
                state,
                notice=f"подзадача {index + 1} не выполнена: {exc}",
            )

        truncated = meta.finish_reason == "length"
        if not meta.content.strip():
            state = task_state.add_section(self.store.state, "", failed=_EXECUTE_FAILED_EMPTY)
            notice = f"подзадача {index + 1}: {_EXECUTE_FAILED_EMPTY}"
        else:
            state = task_state.add_section(self.store.state, meta.content, truncated=truncated)
            notice = f"подзадача {index + 1}: ответ обрезан по потолку запроса" if truncated else ""
        self.store.save(state)
        return self._report(state.active, state, notice=notice, meta=meta)

    def _validation(self, task: TaskItem, state: TaskState) -> TaskStepReport:
        issues = list(deterministic_issues(task))
        new_items: Tuple[str, ...] = ()
        notice = ""
        meta: Optional[AnswerMeta] = None
        try:
            meta, verdict_issues, verdict_items = self._ask_validate(task)
            issues.extend(verdict_issues)
            new_items = verdict_items
        except TaskRequestFailed as exc:
            notice = f"модельная проверка недоступна: {exc}"

        index = state.active_index
        state = task_state.validation_verdict(state, issues, new_items)
        self.store.save(state)
        if state.active_index == index:
            return self._report(state.active, state, notice=notice, meta=meta)

        finished = state.tasks[index]
        if finished.issues:
            notice = "попытки проверки исчерпаны — задача завершается с замечаниями"
        result = task_state.write_result(self.results_dir, index + 1, finished)
        return self._report(
            finished,
            state,
            notice=notice,
            result_path=str(result) if result is not None else "",
            sections=len([section for section in finished.sections if section.text.strip()]),
            artifact_chars=sum(len(section.text) for section in finished.sections),
            issues=tuple(issue.text for issue in finished.issues),
            meta=meta,
            finished_task=True,
            point=self._done_point(state),
            stage=finished.stage,
        )

    # --- запросы к модели ---

    def _ask_plan(self, task: TaskItem) -> Tuple[AnswerMeta, Tuple[str, ...]]:
        messages = self._plan_messages(task)
        meta: Optional[AnswerMeta] = None
        for _ in range(_ASK_ATTEMPTS):
            meta = self._request(messages, PHASE_PLAN)
            plan = parse_plan_response(meta.content)
            if plan:
                return meta, plan
        raise TaskRequestFailed("план не разобран")

    def _ask_execute(self, task: TaskItem, index: int) -> AnswerMeta:
        for _ in range(_ASK_ATTEMPTS):
            meta = self._request(self._execute_messages(task, index), PHASE_EXECUTE)
            if meta.content.strip():
                return meta
        raise TaskRequestFailed(_EXECUTE_FAILED_EMPTY)

    def _ask_validate(
        self, task: TaskItem
    ) -> Tuple[AnswerMeta, Tuple[TaskIssue, ...], Tuple[str, ...]]:
        for _ in range(_ASK_ATTEMPTS):
            meta = self._request(self._validate_messages(task), PHASE_VALIDATE)
            parsed = parse_validation_response(meta.content)
            if parsed is not None:
                return meta, parsed[1], parsed[2]
        raise TaskRequestFailed("вердикт проверки не разобран")

    def _request(self, messages: List[Dict[str, str]], phase: str) -> AnswerMeta:
        try:
            return self._ask(messages, _PHASE_WORDS[phase], phase)
        except APIError as exc:
            raise TaskRequestFailed(str(exc))

    # --- сборка сообщений ---

    def _plan_messages(self, task: TaskItem) -> List[Dict[str, str]]:
        lines = [
            f"Задача пользователя: {task.goal}",
            _plan_size_instruction(task.plan_limit),
            f"Формулировка подзадачи: до {config.TASK_PLAN_ITEM_MAX_CHARS} символов.",
        ]
        if task.replan and task.plan:
            lines.append("Прежний план:")
            lines.extend(f"{number}. {item}" for number, item in enumerate(task.plan, start=1))
        if task.replan and task.edits:
            lines.append(f"Правки пользователя к плану: {task.edits}")
        return [
            {"role": "system", "content": _asset(PLAN_ASSET)},
            {"role": "user", "content": "\n".join(lines)},
        ]

    def _execute_messages(self, task: TaskItem, index: int) -> List[Dict[str, str]]:
        lines = [
            f"Задача пользователя: {task.goal}",
            "План:",
        ]
        lines.extend(f"{number}. {item}" for number, item in enumerate(task.plan, start=1))
        lines.append(f"Твоя подзадача: {index + 1}. {task.plan[index]}")
        lines.append(
            f"Объём раздела: не больше {config.TASK_SECTION_MAX_WORDS} слов — это раздел "
            "рабочего артефакта, а не статья."
        )
        if index in task.fixing:
            # Круг исправления выполняет только незакрытую работу: подзадачу без раздела или с
            # провалившимся разделом и подзадачу, которую назвала проверка. Готовые разделы
            # заново не выполняются — модели нужно знать лишь, из-за чего она здесь.
            if index >= len(task.sections):
                lines.append("Эту подзадачу назвала проверка: в артефакте не хватало её результата.")
            elif task.sections[index].failed:
                lines.append(
                    f"Прошлая попытка выполнить раздел не удалась: {task.sections[index].failed}"
                )
            else:
                lines.append("Раздел остался пустым — напиши его.")
            for issue in task.issues:
                if issue.item in (0, index + 1):
                    lines.append(f"Замечание: {issue.text}")
            lines.append("Выполни подзадачу и напиши её раздел.")
        written = [section for section in task.sections[:index] if section.text.strip()]
        if written:
            lines.append("Уже написанные разделы (не повторяй их):")
            lines.extend(f"— {section.item}: {section.text}" for section in written)
        return [
            {"role": "system", "content": _asset(EXECUTE_ASSET)},
            {"role": "user", "content": "\n".join(lines)},
        ]

    def _validate_messages(self, task: TaskItem) -> List[Dict[str, str]]:
        lines = [f"Задача пользователя: {task.goal}", "План:"]
        lines.extend(f"{number}. {item}" for number, item in enumerate(task.plan, start=1))
        lines.append("Артефакт:")
        lines.append(_artifact_text(task))
        if task.issues:
            lines.append("Замечания прошлой проверки (проверь, закрыты ли они):")
            lines.extend(f"— {issue.text}" for issue in task.issues)
        return [
            {"role": "system", "content": _asset(VALIDATE_ASSET)},
            {"role": "user", "content": "\n".join(lines)},
        ]

    # --- отчёты ---

    def _fail(self, task: TaskItem, state: TaskState, reason: str) -> TaskStepReport:
        state = task_state.fail_task(state, reason)
        self.store.save(state)
        return self._report(
            task,
            state,
            failure=reason,
            finished_task=True,
            point=("итог: задача не удалась", state.expected_action),
            stage=task.stage,
        )

    def _done_point(self, state: TaskState) -> Tuple[str, str]:
        if state.finished:
            return "итог отправлен", task_state.QUEUE_EMPTY_ACTION
        return "итог отправлен", "взять следующую задачу из очереди"

    def _report(
        self,
        task: Optional[TaskItem],
        state: TaskState,
        *,
        meta: Optional[AnswerMeta] = None,
        notice: str = "",
        needs_edits: bool = False,
        finished_task: bool = False,
        sections: int = 0,
        artifact_chars: int = 0,
        issues: Tuple[str, ...] = (),
        failure: str = "",
        result_path: str = "",
        point: Optional[Tuple[str, str]] = None,
        stage: Optional[Stage] = None,
    ) -> TaskStepReport:
        step, action = point if point else (state.current_step, state.expected_action)
        return TaskStepReport(
            goal=task.goal if task else "",
            stage=stage if stage is not None else (task.stage if task else Stage.DONE),
            step=step,
            expected_action=action,
            plan=task.plan if task else (),
            needs_edits=needs_edits,
            finished_task=finished_task,
            sections=sections,
            artifact_chars=artifact_chars,
            issues=issues,
            failure=failure,
            notice=notice,
            result_path=result_path,
            meta=meta,
        )


def _plan_size_instruction(limit: int) -> str:
    """Сколько подзадач просить: диапазон или точное число, если границы совпали."""
    if config.MIN_PLAN_ITEMS >= limit:
        return f"Число подзадач: ровно {config.MIN_PLAN_ITEMS}."
    return f"Число подзадач: от {config.MIN_PLAN_ITEMS} до {limit}."


def _asset(name: str) -> str:
    return (config.ASSETS_DIR / name).read_text(encoding="utf-8").strip()


def _artifact_text(task: TaskItem) -> str:
    blocks: List[str] = []
    for number, item in enumerate(task.plan, start=1):
        if number <= len(task.sections):
            section = task.sections[number - 1]
            body = section.text.strip() or f"— не выполнено: {section.failed or 'причина неизвестна'}"
        else:
            body = "— не выполнено"
        blocks.append(f"## {number}. {item}\n{body}")
    return "\n\n".join(blocks)
