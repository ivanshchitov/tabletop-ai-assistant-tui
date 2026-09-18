"""Состояние задачи агента: конечный автомат Planning → Execution → Validation → Done.

Модуль знает только состояние и его переходы: ни модели, ни терминала, ни HTTP. Каждый переход —
чистая функция над неизменяемым снимком, поэтому автомат проверяется тестами без сети.

Имена этапов оставлены латиницей намеренно: это терминология задачи (planning → execution →
validation → done), и она же печатается пользователю.
"""

import json
import os
import tempfile
from dataclasses import dataclass, replace
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config


class Stage(str, Enum):
    """Этап задачи — состояние автомата, а не оценка прогресса."""

    PLANNING = "planning"
    EXECUTION = "execution"
    VALIDATION = "validation"
    DONE = "done"


STAGES: Tuple[Stage, ...] = (Stage.PLANNING, Stage.EXECUTION, Stage.VALIDATION, Stage.DONE)

STAGE_LABELS: Dict[Stage, str] = {
    Stage.PLANNING: "Planning",
    Stage.EXECUTION: "Execution",
    Stage.VALIDATION: "Validation",
    Stage.DONE: "Done",
}


# Таблица допустимых переходов: единственный источник правды о жизненном цикле задачи. Планирование
# открывает выполнение, выполнение уходит на проверку или возвращается к плану, проверка завершает задачу или
# возвращает работу в выполнение; Done — терминальный этап. Правило живёт кодом, а не текстом промпта: текст
# модель может нарушить, таблицу — нет.
TRANSITIONS: Dict[Stage, Tuple[Stage, ...]] = {
    Stage.PLANNING: (Stage.EXECUTION,),
    Stage.EXECUTION: (Stage.VALIDATION, Stage.PLANNING),
    Stage.VALIDATION: (Stage.DONE, Stage.EXECUTION),
    Stage.DONE: (),
}

# Причины отказа шлюза: их печатает интерфейс и хранит журнал переходов, поэтому они — данные, а не текст в
# месте вызова.
REASON_NO_TASK = "незавершённой задачи нет — менять нечего"
REASON_TERMINAL = "Done — терминальный этап, переходов из него нет"
REASON_SAME_STAGE = "задача уже на этапе {stage}"
REASON_NOT_ALLOWED = "переход {source} → {target} не разрешён таблицей"
REASON_PLAN_NOT_APPROVED = "план не утверждён: реализация до утверждённого плана запрещена"
REASON_NEEDS_VALIDATION = "финал без проверки: в Done можно только из Validation"


class TransitionError(ValueError):
    """Ошибка вызова шлюза: назван этап, которого нет в автомате."""


@dataclass(frozen=True)
class Transition:
    """Запись журнала переходов: попытка смены этапа с её исходом и причиной."""

    source: Stage
    target: Stage
    accepted: bool
    reason: str = ""


@dataclass(frozen=True)
class TransitionResult:
    """Результат шлюза: новое состояние, признак принятия и причина.

    Отказ — данные, а не исключение: он уходит и в журнал переходов, и на экран, а исключение пришлось бы
    ловить в каждой точке конвейера.
    """

    state: "TaskState"
    accepted: bool
    reason: str = ""


class TaskStatus(str, Enum):
    """Статус задачи в очереди; «открытые» статусы делают задачу активной."""

    PENDING = "ожидает"
    RUNNING = "в работе"
    DONE = "решена"
    FAILED = "не удалось"


_OPEN_STATUSES = (TaskStatus.PENDING, TaskStatus.RUNNING)

_TASK_PROMPT_ASSET = "task_prompt.md"

# Чего ждёт Done, когда очередь кончилась: строка печатается и в панели, и в отчёте.
QUEUE_EMPTY_ACTION = "очередь пуста — прогон остановлен"


@dataclass(frozen=True)
class TaskSection:
    """Раздел артефакта — результат одной подзадачи плана.

    Пустой `text` при непустом `failed` означает невыполненную подзадачу: место в артефакте за ней
    сохраняется, чтобы проверка увидела провал по номеру пункта плана.
    """

    item: str
    text: str = ""
    failed: str = ""
    truncated: bool = False


@dataclass(frozen=True)
class TaskIssue:
    """Замечание проверки: номер подзадачи по плану (1-based, 0 — общее) и текст."""

    item: int
    text: str


@dataclass(frozen=True)
class TaskItem:
    """Одна задача очереди: цель, этап, план, артефакт и замечания проверки."""

    goal: str
    status: TaskStatus = TaskStatus.PENDING
    stage: Stage = Stage.PLANNING
    plan: Tuple[str, ...] = ()
    sections: Tuple[TaskSection, ...] = ()
    issues: Tuple[TaskIssue, ...] = ()
    fixing: Tuple[int, ...] = ()
    plan_round: int = 0
    # Потолок числа подзадач у этой задачи: начинается с константы и растёт, когда работы
    # прибавляется — правками пользователя или подзадачами, названными проверкой.
    plan_limit: int = config.MAX_PLAN_ITEMS
    replan: bool = False
    edits: str = ""
    attempt: int = 1
    error: str = ""
    # Журнал переходов задачи: принятые и отклонённые попытки смены этапа. Живёт на задаче, а не на
    # состоянии, потому что жизненный цикл принадлежит задаче и уезжает в файл вместе с ней.
    transitions: Tuple[Transition, ...] = ()


@dataclass(frozen=True)
class TaskState:
    """Состояние конвейера: очередь задач, признак паузы и ожидание правок пользователя."""

    tasks: Tuple[TaskItem, ...] = ()
    paused: bool = False
    awaiting_edits: bool = False

    @property
    def active_index(self) -> int:
        """Индекс активной задачи — первой незавершённой; -1, когда незавершённых нет."""
        for index, task in enumerate(self.tasks):
            if task.status in _OPEN_STATUSES:
                return index
        return -1

    @property
    def active(self) -> Optional[TaskItem]:
        index = self.active_index
        return self.tasks[index] if index >= 0 else None

    @property
    def finished(self) -> bool:
        """Все задачи очереди завершены (или очередь пуста) — прогон останавливается."""
        return self.active is None

    @property
    def current_step(self) -> str:
        """Текущий шаг: круг плана, подзадача, попытка проверки или отправленный итог."""
        task = self.active
        if task is None:
            return "задач нет" if not self.tasks else "незавершённых задач нет"
        return _current_step(task, self.awaiting_edits)

    @property
    def expected_action(self) -> str:
        """Ожидаемое действие: чего ждёт агент или что произойдёт на этом шаге."""
        task = self.active
        if task is None:
            if not self.tasks:
                return "поставить задачу командой /task add"
            return QUEUE_EMPTY_ACTION
        return _expected_action(task, self.awaiting_edits)


def clip_goal(goal: str, limit: int = config.TASK_GOAL_MAX_CHARS) -> str:
    """Обрезает цель задачи: она уходит и в состояние, и в сообщение каждого запроса."""
    return goal.strip()[:limit]


def next_item_index(task: TaskItem) -> Optional[int]:
    """Индекс подзадачи к выполнению: сперва очередь исправления, затем следующая по плану."""
    if task.fixing:
        return task.fixing[0]
    if len(task.sections) < len(task.plan):
        return len(task.sections)
    return None


def _current_step(task: TaskItem, awaiting_edits: bool) -> str:
    if task.stage is Stage.PLANNING:
        if awaiting_edits:
            return f"круг {task.plan_round}, подзадач {len(task.plan)} — жду правок"
        if task.replan or not task.plan:
            return f"круг {task.plan_round + 1}: план не построен"
        return "план принят"
    if task.stage is Stage.EXECUTION:
        index = next_item_index(task)
        if index is None:
            return "все подзадачи выполнены — иду в Validation"
        return f"{index + 1}/{len(task.plan)}: «{task.plan[index]}»"
    if task.stage is Stage.VALIDATION:
        return f"попытка {task.attempt}/{config.MAX_VALIDATION_ATTEMPTS}"
    return "итог отправлен"


def _expected_action(task: TaskItem, awaiting_edits: bool) -> str:
    if task.stage is Stage.PLANNING:
        if awaiting_edits:
            return "дождаться правок пользователя к плану"
        if task.replan or not task.plan:
            return "построить план задачи"
        return "принять план и перейти к Execution"
    if task.stage is Stage.EXECUTION:
        index = next_item_index(task)
        if index is None:
            return "перейти к проверке артефакта"
        if index in task.fixing:
            return f"исправить по замечаниям: «{task.plan[index]}»"
        return f"выполнить подзадачу «{task.plan[index]}»"
    if task.stage is Stage.VALIDATION:
        return "сверить артефакт с планом и получить вердикт"
    return "взять следующую задачу из очереди"


# --- шлюз переходов ---


def allowed_transitions(stage: Stage) -> Tuple[Stage, ...]:
    """Разрешённые из этапа переходы: их называют отчёт, отказ шлюза и сообщение модели."""
    return TRANSITIONS.get(stage, ())


def parse_stage(name: str) -> Optional[Stage]:
    """Этап по имени (любой регистр); None — такого этапа в автомате нет."""
    try:
        return Stage(str(name).strip().lower())
    except ValueError:
        return None


def transition(state: TaskState, target: Stage, reason: str = "") -> TransitionResult:
    """Единственный шлюз смены этапа: таблица переходов плюс предусловия целевого этапа.

    Любая смена этапа — конвейером или по просьбе пользователя — проходит здесь, поэтому пропустить этап
    нельзя ни одному вызывающему. Отказ не меняет состояние и возвращает причину; исключение поднимается
    только на неизвестный этап — это ошибка вызова, а не решение автомата.
    """
    if not isinstance(target, Stage):
        parsed = parse_stage(target)
        if parsed is None:
            raise TransitionError(f"этапа «{target}» в автомате нет")
        target = parsed
    task = state.active
    if task is None:
        return TransitionResult(state, False, REASON_NO_TASK)
    source = task.stage
    refusal = _refusal(state, task, source, target)
    accepted = not refusal
    updated = replace(
        task,
        stage=target if accepted else source,
        transitions=_log_transition(
            task.transitions, Transition(source, target, accepted, refusal or reason)
        ),
    )
    return TransitionResult(_put_active(state, updated), accepted, refusal or reason)


def _log_transition(
    journal: Tuple[Transition, ...], entry: Transition
) -> Tuple[Transition, ...]:
    """Дописывает запись в журнал, вытесняя самые старые сверх предела."""
    return (journal + (entry,))[-config.MAX_TRANSITION_LOG :]


def _refusal(state: TaskState, task: "TaskItem", source: Stage, target: Stage) -> str:
    """Причина отказа шлюза или пустая строка, когда переход разрешён."""
    if source is target:
        return REASON_SAME_STAGE.format(stage=STAGE_LABELS[source])
    if source is Stage.DONE:
        return REASON_TERMINAL
    if target not in allowed_transitions(source):
        if target is Stage.DONE:
            return REASON_NEEDS_VALIDATION
        return REASON_NOT_ALLOWED.format(
            source=STAGE_LABELS[source], target=STAGE_LABELS[target]
        )
    if target is Stage.EXECUTION and not _plan_approved(state, task):
        return REASON_PLAN_NOT_APPROVED
    return ""


def _plan_approved(state: TaskState, task: "TaskItem") -> bool:
    """План утверждён: он построен, правки не ожидаются и перестроение не запрошено."""
    return bool(task.plan) and not state.awaiting_edits and not task.replan


# --- переходы автомата ---


def add_task(state: TaskState, goal: str) -> TaskState:
    """Ставит задачу в конец очереди; пустая цель задачу не заводит."""
    clipped = clip_goal(goal)
    if not clipped:
        return state
    return replace(state, tasks=state.tasks + (TaskItem(goal=clipped),))


def drop_all(state: TaskState) -> TaskState:
    """Снимает очередь целиком: задача — это не диалог, снимает её только команда пользователя."""
    return TaskState()


def set_paused(state: TaskState, paused: bool) -> TaskState:
    """Ставит и снимает паузу: признак живёт в состоянии, поэтому переживает перезапуск."""
    return replace(state, paused=paused)


def begin_task(state: TaskState) -> TaskState:
    """Отмечает активную задачу как взятую в работу (запрос плана состоялся)."""
    return _replace_active(state, status=TaskStatus.RUNNING)


def plan_built(state: TaskState, plan: Tuple[str, ...]) -> TaskState:
    """Записывает построенный план и переводит Planning в ожидание правок.

    Первый план ограничен потолком задачи: неразумно длинный план растянул бы прогон, а
    подзадачи сверх потолка всё равно не влезли бы в разумное число разделов артефакта. План,
    перестроенный по правкам пользователя, потолок поднимает: работы стало больше по его просьбе,
    и терять подзадачи из ответа нельзя.
    """
    items = tuple(item.strip() for item in plan if item.strip())
    limit = max(task_limit(state), len(items)) if _is_replan(state) else task_limit(state)
    items = items[:limit]
    task = state.active
    if task is None or not items:
        return state
    updated = replace(
        task,
        status=TaskStatus.RUNNING,
        plan=items,
        plan_limit=limit,
        sections=(),
        issues=(),
        fixing=(),
        replan=False,
        plan_round=task.plan_round + 1,
    )
    return _put_active(replace(state, awaiting_edits=True), updated)


def edits_response(state: TaskState, text: str) -> TaskState:
    """Принимает ответ пользователя о правках: непустой просит новый круг, пустой идёт дальше.

    На исчерпанных кругах планирования правки больше не принимаются — план идёт как есть, иначе
    нестабильный пользователь или модель гоняли бы Planning бесконечно.
    """
    task = state.active
    if task is None or not state.awaiting_edits:
        return state
    if text.strip() and task.plan_round < config.MAX_PLAN_ROUNDS:
        edits = text.strip()[: config.TASK_EDITS_MAX_CHARS]
        return _put_active(
            replace(state, awaiting_edits=False), replace(task, replan=True, edits=edits)
        )
    cleared = _put_active(replace(state, awaiting_edits=False), replace(task, replan=False))
    return _advance(cleared, Stage.EXECUTION, "правок нет — план принят")


def accept_plan(state: TaskState) -> TaskState:
    """Завершает Planning: правок нет, задача идёт в Execution."""
    task = state.active
    if task is None:
        return state
    cleared = _put_active(replace(state, awaiting_edits=False), replace(task, replan=False))
    return _advance(cleared, Stage.EXECUTION, "план принят")


def add_section(state: TaskState, text: str, failed: str = "", truncated: bool = False) -> TaskState:
    """Дописывает (или при исправлении переписывает) раздел артефакта текущей подзадачи."""
    task = state.active
    if task is None:
        return state
    index = next_item_index(task)
    if index is None:
        return state
    section = TaskSection(
        item=task.plan[index], text=text.strip(), failed=failed, truncated=truncated
    )
    if index == len(task.sections):
        sections = task.sections + (section,)
    else:
        sections = task.sections[:index] + (section,) + task.sections[index + 1 :]
    fixing = tuple(number for number in task.fixing if number != index)
    return _put_active(state, replace(task, sections=sections, fixing=fixing))


def start_validation(state: TaskState) -> TaskState:
    """Переводит задачу на этап проверки артефакта."""
    return _advance(state, Stage.VALIDATION, "все подзадачи выполнены")


def validation_verdict(
    state: TaskState, issues: List[TaskIssue], new_items: Tuple[str, ...] = ()
) -> TaskState:
    """Применяет вердикт проверки: без замечаний задача Done, с замечаниями — исправление.

    В круг исправления попадает только незакрытая работа: подзадачи без раздела, с провалившимся
    разделом или с пустым разделом, плюс подзадачи, которые проверка назвала новыми. Уже готовые
    разделы заново не выполняются — сделанное не переделывают. Замечания к готовым разделам
    остаются в списке и попадают в итог, если задача завершается с ними.

    План растёт только в круг исправления и не больше предела за круг: иначе нестабильный ответ
    модели растягивал бы прогон, а проверки ходили бы по кругу.
    """
    task = state.active
    if task is None:
        return state
    if not issues and not new_items:
        finished = _advance(
            replace(state, awaiting_edits=False), Stage.DONE, "проверка без замечаний"
        )
        return _replace_active(finished, status=TaskStatus.DONE, issues=(), fixing=())
    additions = tuple(
        item.strip()[: config.TASK_PLAN_ITEM_MAX_CHARS]
        for item in new_items
        if item.strip()
    )
    incomplete = {index for index in incomplete_indexes(task)}
    fixing = incomplete | set(range(len(task.plan), len(task.plan) + len(additions)))
    if fixing and task.attempt < config.MAX_VALIDATION_ATTEMPTS:
        fixed = _advance(
            replace(state, awaiting_edits=False),
            Stage.EXECUTION,
            "замечания проверки — круг исправления",
        )
        return _replace_active(
            fixed,
            plan=task.plan + additions,
            plan_limit=max(task.plan_limit, len(task.plan) + len(additions)),
            attempt=task.attempt + 1,
            issues=tuple(issues),
            fixing=tuple(sorted(fixing)),
        )
    finished = _advance(
        replace(state, awaiting_edits=False), Stage.DONE, "попытки проверки исчерпаны"
    )
    return _replace_active(
        finished, status=TaskStatus.DONE, issues=tuple(issues), fixing=()
    )


def task_limit(state: TaskState) -> int:
    """Потолок числа подзадач активной задачи (по умолчанию — константа)."""
    task = state.active
    return task.plan_limit if task is not None else config.MAX_PLAN_ITEMS


def _is_replan(state: TaskState) -> bool:
    task = state.active
    return bool(task is not None and task.replan)


def incomplete_indexes(task: TaskItem) -> Tuple[int, ...]:
    """Подзадачи без готового раздела: их и выполняет круг исправления.

    Готовым считается непустой раздел (обрезанный по потолку — тоже готовый: работа сделана,
    замечание о ней уходит в итог, а не в повторное выполнение).
    """
    return tuple(
        index
        for index, _ in enumerate(task.plan)
        if index >= len(task.sections)
        or task.sections[index].failed
        or not task.sections[index].text.strip()
    )


def fail_task(state: TaskState, reason: str) -> TaskState:
    """Помечает активную задачу не удавшейся: очередь идёт к следующей задаче."""
    task = state.active
    if task is None:
        return state
    return _put_active(
        replace(state, awaiting_edits=False), replace(task, status=TaskStatus.FAILED, error=reason)
    )


def _advance(state: TaskState, target: Stage, reason: str) -> TaskState:
    """Смена этапа конвейером: тот же шлюз, что и у запроса пользователя.

    Отказ шлюза оставляет состояние прежним и не роняет прогон — попытка остаётся в журнале
    переходов, а конвейер повторит операцию на следующем шаге.
    """
    return transition(state, target, reason).state


def _replace_active(state: TaskState, **changes) -> TaskState:
    task = state.active
    if task is None:
        return state
    return _put_active(state, replace(task, **changes))


def _put_active(state: TaskState, task: TaskItem) -> TaskState:
    index = state.active_index
    if index < 0:
        return state
    return replace(state, tasks=state.tasks[:index] + (task,) + state.tasks[index + 1 :])


# --- сообщение модели ---


@lru_cache(maxsize=None)
def task_instruction() -> str:
    """Инструкция работы с состоянием задачи из ассета assets/task_prompt.md."""
    return (config.ASSETS_DIR / _TASK_PROMPT_ASSET).read_text(encoding="utf-8").strip()


def _transitions_line(stage: Stage) -> str:
    """Разрешённые из этапа переходы строкой для модели: запрещённые не перечисляем.

    Список запрещённых переходов приглашал бы искать обход, как список запрещённых слов у
    инвариантов; модели называют только то, что можно, а отказ всё равно за кодом.
    """
    targets = allowed_transitions(stage)
    if not targets:
        return "переходов нет, это последний этап"
    return ", ".join(STAGE_LABELS[target] for target in targets)


def task_message(state: TaskState) -> Optional[str]:
    """Системное сообщение состояния задачи для запроса к модели.

    Пустая очередь и очередь без незавершённых задач сообщения не дают: форма запроса без задачи
    не меняется, как и у пустого профиля с пустыми слоями памяти.
    """
    task = state.active
    if task is None:
        return None
    done = sum(
        1
        for section in task.sections
        if section.text and not section.failed
    )
    lines = [
        task_instruction(),
        "",
        f"Задача пользователя: «{task.goal}»",
        f"Этап: {STAGE_LABELS[task.stage]}",
        f"Текущий шаг: {state.current_step}",
        f"Ожидаемое действие: {state.expected_action}",
        f"Разрешённый переход с этого этапа: {_transitions_line(task.stage)}",
    ]
    if task.plan:
        lines.append(f"План: {len(task.plan)} подзадач, выполнено {done}")
    if state.paused:
        lines.append("Задача на паузе: пользователь вернётся к ней позже.")
    return "\n".join(lines)


# --- результат задачи ---

# Транслитерация для имени файла: кириллица в именах файлов на macOS нормализуется по-своему,
# и сравнение имён в тестах становится хрупким. Латинская слога от этого свободна.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def result_slug(goal: str, limit: int = config.TASK_RESULT_SLUG_MAX_CHARS) -> str:
    """Слога из цели задачи: латиница, дефисы вместо пробелов и знаков."""
    letters: List[str] = []
    for char in goal.lower():
        if char.isascii() and char.isalnum():
            letters.append(char)
        elif char in _TRANSLIT:
            letters.append(_TRANSLIT[char])
        else:
            letters.append("-")
    slug = "".join(letters).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:limit].strip("-") or "task"


def result_file_name(task: TaskItem, number: int) -> str:
    """Имя файла результата: номер задачи в очереди и слога из её цели."""
    return f"{number}-{result_slug(task.goal)}.md"


def result_markdown(task: TaskItem) -> str:
    """Результат задачи: цель, итог и разделы артефакта.

    Фразы без склонений («Разделов: 9») — русские числительные собирает терминальный слой, а
    файл результата пишется из ядра и должен читаться одинаково при любом числе разделов.
    """
    written = [section for section in task.sections if section.text.strip()]
    head = [f"# {task.goal}", ""]
    if task.issues:
        head.append(
            f"_Задача завершена с замечаниями: разделов — {len(written)}, "
            f"символов — {sum(len(section.text) for section in written)}._"
        )
    else:
        head.append(
            f"_Задача решена: разделов — {len(written)}, "
            f"символов — {sum(len(section.text) for section in written)}._"
        )
    head.append("")
    body: List[str] = []
    for number, item in enumerate(task.plan, start=1):
        if number > len(task.sections):
            text = "_раздел не написан_"
        else:
            section = task.sections[number - 1]
            text = section.text.strip() or f"_не выполнено: {section.failed or 'причина неизвестна'}_"
        body.append(f"## {number}. {item}")
        body.append("")
        body.append(text)
        body.append("")
    tail: List[str] = []
    if task.issues:
        tail.append("## Замечания проверки")
        tail.append("")
        tail.extend(f"- {issue.text}" for issue in task.issues)
        tail.append("")
    return "\n".join(head + body + tail).rstrip() + "\n"


def write_result(directory: Path, number: int, task: TaskItem) -> Optional[Path]:
    """Пишет результат задачи в каталог `tasks/`; ошибка записи не роняет прогон."""
    path = directory / result_file_name(task, number)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(result_markdown(task), encoding="utf-8")
    except OSError:
        return None
    return path


# --- хранилище ---


class TaskStore:
    """Файл состояния задачи: своя очередь, свой срок жизни, свой переключатель пути.

    Задача не является ни диалогом, ни сведениями о пользователе, поэтому `/clear` её не касается.
    Состояние пишется атомарно (временный файл рядом и `os.replace`) — прерывание в момент записи
    не оставляет обрезанный файл.
    """

    def __init__(self, path: Path = config.TASK_FILE):
        self.path = path
        self.state = self._load()

    def _load(self) -> TaskState:
        """Читает файл; нет файла, битый JSON или чужая форма — пустая очередь, без ошибки."""
        if not self.path.exists():
            return TaskState()
        try:
            with self.path.open("r", encoding="utf-8") as file:
                data = json.load(file)
        except (json.JSONDecodeError, OSError, ValueError):
            return TaskState()
        if not isinstance(data, dict):
            return TaskState()
        raw_tasks = data.get("tasks")
        tasks: List[TaskItem] = []
        if isinstance(raw_tasks, list):
            for raw in raw_tasks:
                if isinstance(raw, dict):
                    tasks.append(_task_from_dict(raw))
        return TaskState(
            tasks=tuple(tasks),
            paused=bool(data.get("paused")),
            awaiting_edits=bool(data.get("awaiting_edits")),
        )

    def save(self, state: TaskState) -> None:
        """Записывает состояние на диск; ошибка записи не роняет прогон."""
        payload = {
            "version": 1,
            "paused": state.paused,
            "awaiting_edits": state.awaiting_edits,
            "tasks": [_task_to_dict(task) for task in state.tasks],
        }
        self.state = state
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=self.path.name + ".", suffix=".tmp"
            )
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
            os.replace(temporary, self.path)
        except OSError:
            pass


def _task_to_dict(task: TaskItem) -> Dict[str, object]:
    return {
        "goal": task.goal,
        "status": task.status.value,
        "stage": task.stage.value,
        "plan": list(task.plan),
        "sections": [
            {
                "item": section.item,
                "text": section.text,
                "failed": section.failed,
                "truncated": section.truncated,
            }
            for section in task.sections
        ],
        "issues": [{"item": issue.item, "text": issue.text} for issue in task.issues],
        "fixing": list(task.fixing),
        "plan_round": task.plan_round,
        "plan_limit": task.plan_limit,
        "replan": task.replan,
        "edits": task.edits,
        "attempt": task.attempt,
        "error": task.error,
        "transitions": [
            {
                "source": entry.source.value,
                "target": entry.target.value,
                "accepted": entry.accepted,
                "reason": entry.reason,
            }
            for entry in task.transitions
        ],
    }


def _task_from_dict(raw: Dict[str, object]) -> TaskItem:
    sections: List[TaskSection] = []
    for entry in raw.get("sections") or []:
        if isinstance(entry, dict):
            sections.append(
                TaskSection(
                    item=str(entry.get("item", "")),
                    text=str(entry.get("text", "")),
                    failed=str(entry.get("failed", "")),
                    truncated=bool(entry.get("truncated")),
                )
            )
    issues: List[TaskIssue] = []
    for entry in raw.get("issues") or []:
        if isinstance(entry, dict):
            issues.append(TaskIssue(item=_as_int(entry.get("item")), text=str(entry.get("text", ""))))
    transitions: List[Transition] = []
    for entry in raw.get("transitions") or []:
        if isinstance(entry, dict):
            transitions.append(
                Transition(
                    source=_stage(entry.get("source")),
                    target=_stage(entry.get("target")),
                    accepted=bool(entry.get("accepted")),
                    reason=str(entry.get("reason", "")),
                )
            )
    plan = tuple(str(item) for item in raw.get("plan") or [])
    fixing = tuple(_as_int(index) for index in raw.get("fixing") or [])
    return TaskItem(
        goal=str(raw.get("goal", "")),
        status=_status(raw.get("status")),
        stage=_stage(raw.get("stage")),
        plan=plan,
        sections=tuple(sections),
        issues=tuple(issues),
        fixing=fixing,
        plan_round=_as_int(raw.get("plan_round")),
        plan_limit=max(config.MIN_PLAN_ITEMS, _as_int(raw.get("plan_limit"), config.MAX_PLAN_ITEMS)),
        replan=bool(raw.get("replan")),
        edits=str(raw.get("edits", "")),
        attempt=max(1, _as_int(raw.get("attempt"), 1)),
        error=str(raw.get("error", "")),
        transitions=tuple(transitions),
    )


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _status(value: object) -> TaskStatus:
    try:
        return TaskStatus(str(value))
    except ValueError:
        return TaskStatus.PENDING


def _stage(value: object) -> Stage:
    try:
        return Stage(str(value))
    except ValueError:
        return Stage.PLANNING
