"""Выбор инструмента MCP моделью: каталог инструментов, разбор ответа, сообщение результата.

Чистая часть автоматического вызова — без сети и без HTTP, по образцу `context_strategies`
для блока фактов: агент оркестрирует запросы, а модуль решает, что уходит в запрос выбора и
как читается ответ.

Ответ модели разбирается на стороне приложения (первый JSON-объект в тексте), а не полем
`response_format` запроса: reasoning-модели пула на нём ненадёжны — ровно та же причина, по
которой так устроены извлекатель фактов и разбор плана задачи.

Имена инструментов здесь не упоминаются: каталог целиком строится из снимков подключения,
поэтому замена сервера остаётся правкой одной записи реестра.
"""

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config

TOOL_CHOICE_ASSET = "tool_choice_prompt.md"
TOOL_RESULT_ASSET = "tool_result_prompt.md"
TOOL_FLOW_ASSET = "tool_flow_prompt.md"

CHOICE_USER_TEMPLATE = (
    "Доступные инструменты:\n{catalog}\n\n---\n\nВопрос пользователя:\n{question}"
)

RESULT_MESSAGE_TEMPLATE = (
    "Данные внешнего источника, полученные для текущего вопроса.\n"
    "Инструмент «{tool}» сервера «{server}», аргументы: {arguments}.\n\n{text}\n\n{instruction}"
)

CHAIN_MESSAGE_TEMPLATE = (
    "Данные внешних источников, полученные для текущего вопроса цепочкой инструментов "
    "(шагов: {count}).\n\n{steps}\n\n{instruction}"
)
CHAIN_STEP_TEMPLATE = "Шаг {number}: инструмент «{tool}» сервера «{server}», аргументы: {arguments}.\n{text}"

ROUND_USER_TEMPLATE = (
    "Доступные инструменты:\n{catalog}\n\n---\n\nВопрос пользователя:\n{question}\n\n---\n\n"
    "Выполненные шаги ({count}):\n\n{steps}\n\n---\n\nПервый шаг этого раунда получит номер {next}."
)
ROUND_STEP_TEMPLATE = "Шаг {number}: инструмент «{tool}» сервера «{server}», аргументы: {arguments}.\n{text}"
CLIPPED_NOTE = "\n… сокращено: показано {shown} из {total} симв."

# Причины завершения флоу — для снимка и отчёта `/tool flow`.
FLOW_NO_TOOL = "инструменты не понадобились"
FLOW_DONE = "модель завершила флоу"
FLOW_ROUNDS_LIMIT = "исчерпан предел раундов ({limit})"
FLOW_STEPS_LIMIT = "исчерпан предел шагов ({limit})"
FLOW_STEP_FAILED = "сбой шага {number}"
FLOW_CHOICE_FAILED = "сбой выбора в раунде {round}: {error}"

NO_ARGUMENTS = "без аргументов"
# Ссылка на результат выполненного шага: аргумент, целиком равный «$N».
_REFERENCE_RE = re.compile(r"^\$(\d+)$")

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class ToolChoice:
    """Что модель выбрала: сервер, инструмент и аргументы вызова."""

    server: str
    tool: str
    arguments: Dict[str, Any]


class _Unparsed:
    """Ответ выбора прочитать не удалось — это сбой, а не «инструмент не нужен»."""

    def __repr__(self) -> str:  # для читаемого вывода в отчётах теста
        return "UNPARSED"


# Отличать «модель сказала, что инструмент не нужен» от «ответ не разобран» обязательно:
# первое — обычный исход, второе — сбой, о котором пользователь узнаёт строкой журнала.
UNPARSED = _Unparsed()


@lru_cache(maxsize=None)
def choice_instruction() -> str:
    """Инструкция выбора инструмента из ассета assets/tool_choice_prompt.md."""
    return (config.ASSETS_DIR / TOOL_CHOICE_ASSET).read_text(encoding="utf-8").strip()


@lru_cache(maxsize=None)
def flow_instruction() -> str:
    """Инструкция следующего раунда выбора из ассета assets/tool_flow_prompt.md."""
    return (config.ASSETS_DIR / TOOL_FLOW_ASSET).read_text(encoding="utf-8").strip()


@lru_cache(maxsize=None)
def result_instruction() -> str:
    """Инструкция об использовании данных инструмента из assets/tool_result_prompt.md."""
    return (config.ASSETS_DIR / TOOL_RESULT_ASSET).read_text(encoding="utf-8").strip()


def render_catalog(reports: Iterable[Any]) -> str:
    """Каталог инструментов по снимкам подключения: сервер, инструмент, его параметры.

    Сервер, который не поднялся, в каталог не попадает: предлагать модели инструмент, вызвать
    который заведомо нельзя, значит тратить запрос на неизбежную ошибку.
    """
    blocks: List[str] = []
    for report in reports:
        if getattr(report, "error", "") or not getattr(report, "tools", ()):
            continue
        lines = [f"Сервер «{report.spec_name}»:"]
        for tool in report.tools:
            lines.append(f"- {tool.name}: {tool.description}")
            for parameter in tool.parameters():
                lines.append(f"    - {_render_parameter(parameter)}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _render_parameter(parameter: Any) -> str:
    parts = [parameter.name]
    if parameter.type:
        parts.append(f"({parameter.type})")
    parts.append("— обязательный" if parameter.required else "— необязательный")
    if parameter.description:
        parts.append(parameter.description)
    if parameter.allowed:
        parts.append("возможные значения: " + ", ".join(parameter.allowed))
    if parameter.default is not None:
        parts.append(f"по умолчанию {parameter.default}")
    return " ".join(parts)


def build_choice_messages(reports: Iterable[Any], question: str) -> List[Dict[str, str]]:
    """Запрос выбора: system — инструкция, user — каталог инструментов и вопрос."""
    return [
        {"role": "system", "content": choice_instruction()},
        {
            "role": "user",
            "content": CHOICE_USER_TEMPLATE.format(
                catalog=render_catalog(reports), question=question
            ),
        },
    ]


def build_round_messages(
    reports: Iterable[Any], question: str, done: Iterable[Any]
) -> List[Dict[str, str]]:
    """Запрос следующего раунда: инструкция выбора и раунда, каталог, вопрос, выполненные шаги.

    Шаг — объект с полями `step`, `round`, `server`, `tool`, `arguments`, `sources`, `text`.
    Результаты укладываются в `TOOL_FLOW_CONTEXT_CHARS` от нового шага к старому: свежий
    результат — то, с чем модель работает сейчас, поэтому режутся старые, а шаги прошлых
    раундов не длиннее `TOOL_FLOW_OLD_RESULT_CHARS`. Подстановку `$N` бюджет не трогает.
    """
    done = list(done)
    budget = config.TOOL_FLOW_CONTEXT_CHARS
    latest = max((step.round for step in done), default=0)
    shown: Dict[int, str] = {}
    for step in reversed(done):
        limit = budget if step.round == latest else min(budget, config.TOOL_FLOW_OLD_RESULT_CHARS)
        take = min(len(step.text), limit)
        budget -= take
        text = step.text[:take]
        if take < len(step.text):
            text += CLIPPED_NOTE.format(shown=take, total=len(step.text))
        shown[step.step] = text
    blocks = [
        ROUND_STEP_TEMPLATE.format(
            number=step.step,
            tool=step.tool,
            server=step.server,
            arguments=render_arguments(step.arguments, step.sources),
            text=shown[step.step],
        )
        for step in done
    ]
    return [
        {"role": "system", "content": choice_instruction() + "\n\n" + flow_instruction()},
        {
            "role": "user",
            "content": ROUND_USER_TEMPLATE.format(
                catalog=render_catalog(reports),
                question=question,
                count=len(blocks),
                steps="\n\n".join(blocks),
                next=len(done) + 1,
            ),
        },
    ]


@dataclass(frozen=True)
class RoundChoice:
    """Ответ раунда выбора: шаги в пределах потолка, сколько отброшено, нужен ли ещё раунд."""

    steps: Tuple[ToolChoice, ...]
    dropped: int
    more: bool


def parse_round(text: str) -> Any:
    """Разобрать ответ раунда: `RoundChoice`, None (флоу завершён) или `UNPARSED`.

    `{"done": true}`, `{"tool": null}` и пустой список шагов завершают флоу; следующий раунд
    нужен только при явном `"more": true` — без пометки флоу стоит одного запроса выбора.
    """
    data = _first_json_object(text or "")
    if data is None:
        return UNPARSED
    if data.get("done") is True:
        return None
    chain = parse_chain(text)
    if chain is None or chain is UNPARSED:
        return chain
    steps, dropped = chain
    return RoundChoice(steps=steps, dropped=dropped, more=data.get("more") is True)


def parse_choice(text: str) -> Any:
    """Разобрать ответ выбора: `ToolChoice`, None («инструмент не нужен») или `UNPARSED`."""
    data = _first_json_object(text or "")
    if data is None:
        return UNPARSED
    tool = data.get("tool")
    if not isinstance(tool, str) or not tool.strip():
        return None
    arguments = data.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    server = data.get("server")
    return ToolChoice(
        server=str(server).strip() if isinstance(server, str) else "",
        tool=tool.strip(),
        arguments=arguments,
    )


@dataclass(frozen=True)
class Route:
    """Куда уходит вызов шага: сервер, сервер, названный моделью (если маршрут исправлен), ошибка."""

    server: str
    rerouted_from: str = ""
    error: str = ""


def route(reports: Iterable[Any], server: str, tool: str) -> Route:
    """Выбрать сервер шага по каталогу — детерминированно, без обращения к модели.

    Кандидаты — подключённые серверы, объявившие инструмент. Названный моделью кандидат
    принимается как есть; единственный кандидат исправляет неверно названный сервер; ни одного
    кандидата или несколько без названного среди них — ошибка шага, процесс не запускается.
    """
    candidates = [
        report.spec_name
        for report in reports
        if not report.error and any(declared.name == tool for declared in report.tools)
    ]
    if server and server in candidates:
        return Route(server=server)
    if not candidates:
        return Route(server="", error=f"инструмент «{tool}» не объявлен ни одним сервером")
    if len(candidates) > 1:
        return Route(
            server="",
            error=f"неоднозначно: инструмент «{tool}» объявлен серверами {', '.join(candidates)}",
        )
    return Route(server=candidates[0], rerouted_from=server)


class ReferenceFailure(ValueError):
    """Аргумент ссылается на шаг, который ещё не выполнен: вызывать инструмент нельзя."""


def parse_chain(text: str) -> Any:
    """Разобрать ответ выбора как цепочку: (шаги, отброшено), None или `UNPARSED`.

    Цепочка — `{"steps": [{"server", "tool", "arguments"}, ...]}`; прежний ответ с одним
    инструментом — цепочка из одного шага. Шаги сверх `TOOL_CHAIN_MAX_STEPS` отбрасываются,
    их число возвращается, чтобы журнал мог о нём сказать.
    """
    data = _first_json_object(text or "")
    if data is None:
        return UNPARSED
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list):
        single = parse_choice(text)
        return None if single is None else ((single,), 0)
    steps = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        step = parse_choice(json.dumps(raw, ensure_ascii=False))
        if isinstance(step, ToolChoice):
            steps.append(step)
    if not steps:
        return None
    limit = config.TOOL_CHAIN_MAX_STEPS
    return tuple(steps[:limit]), max(0, len(steps) - limit)


def resolve_references(
    arguments: Dict[str, Any], results: List[str]
) -> Tuple[Dict[str, Any], Dict[str, Tuple[int, ...]]]:
    """Подставить результаты выполненных шагов вместо ссылок `$N`.

    Подстановка дословная: модель данные не перепечатывает, поэтому то, что вернул шаг N,
    доходит до следующего инструмента без усечения и пересказа. Ссылка — всё значение целиком
    или отдельная строка многострочного значения (так итог собирается из нескольких шагов);
    `$N` внутри строки вместе с другим текстом остаётся как есть — иначе «цена $5» дала бы
    случайную подстановку. Возвращает аргументы и карту «аргумент → номера шагов-источников».
    """
    resolved: Dict[str, Any] = {}
    sources: Dict[str, Tuple[int, ...]] = {}
    for key, value in arguments.items():
        if not isinstance(value, str):
            resolved[key] = value
            continue
        lines = value.split("\n")
        used: List[int] = []
        for index, line in enumerate(lines):
            match = _REFERENCE_RE.match(line.strip())
            if match is None:
                continue
            number = int(match.group(1))
            if not 1 <= number <= len(results):
                raise ReferenceFailure(
                    f"аргумент {key} ссылается на шаг {number}, а выполнено шагов: {len(results)}"
                )
            lines[index] = results[number - 1]
            used.append(number)
        if used:
            resolved[key] = "\n".join(lines)
            sources[key] = tuple(used)
        else:
            resolved[key] = value
    return resolved, sources


def _first_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Первый JSON-объект в тексте: модель любит обрамлять ответ пояснениями и ```-блоками."""
    candidates = _FENCED_JSON_RE.findall(text)
    match = _JSON_OBJECT_RE.search(text)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def render_arguments(
    arguments: Dict[str, Any], sources: Optional[Dict[str, Tuple[int, ...]]] = None
) -> str:
    """Аргументы одной строкой — и для журнала, и для сообщения модели.

    Аргумент, полученный по ссылке, пишется ссылкой на шаг: переданный текст уже есть в
    результате шага-источника, и повторять его значило бы удвоить объём запроса.
    """
    if not arguments:
        return NO_ARGUMENTS
    sources = sources or {}
    return ", ".join(
        f"{key}={source_label(sources[key])}" if key in sources else f"{key}={value}"
        for key, value in sorted(arguments.items())
    )


def source_label(steps: Tuple[int, ...]) -> str:
    """«← шаг N» для одной ссылки, «← шаги N, M» для итога, собранного из нескольких."""
    if len(steps) == 1:
        return f"← шаг {steps[0]}"
    return "← шаги " + ", ".join(str(step) for step in steps)


def tool_result_message(
    server: str, tool: str, arguments: Dict[str, Any], text: str
) -> str:
    """Системное сообщение с результатом вызова: что вызвали, с чем и что ответил источник."""
    return RESULT_MESSAGE_TEMPLATE.format(
        tool=tool,
        server=server,
        arguments=render_arguments(arguments),
        text=text,
        instruction=result_instruction(),
    )


def tool_chain_message(steps: Iterable[Tuple[str, str, Dict[str, Any], Dict[str, int], str]]) -> str:
    """Системное сообщение с результатами цепочки: каждый шаг — что вызвали и что вернулось.

    Шаг — кортеж (сервер, инструмент, аргументы, источники ссылок, текст результата).
    """
    blocks = [
        CHAIN_STEP_TEMPLATE.format(
            number=number,
            tool=tool,
            server=server,
            arguments=render_arguments(arguments, sources),
            text=text,
        )
        for number, (server, tool, arguments, sources, text) in enumerate(steps, start=1)
    ]
    return CHAIN_MESSAGE_TEMPLATE.format(
        count=len(blocks), steps="\n\n".join(blocks), instruction=result_instruction()
    )
