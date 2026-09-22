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
from typing import Any, Dict, Iterable, List, Optional

from . import config

TOOL_CHOICE_ASSET = "tool_choice_prompt.md"
TOOL_RESULT_ASSET = "tool_result_prompt.md"

CHOICE_USER_TEMPLATE = (
    "Доступные инструменты:\n{catalog}\n\n---\n\nВопрос пользователя:\n{question}"
)

RESULT_MESSAGE_TEMPLATE = (
    "Данные внешнего источника, полученные для текущего вопроса.\n"
    "Инструмент «{tool}» сервера «{server}», аргументы: {arguments}.\n\n{text}\n\n{instruction}"
)

NO_ARGUMENTS = "без аргументов"

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


def render_arguments(arguments: Dict[str, Any]) -> str:
    """Аргументы одной строкой — и для журнала, и для сообщения модели."""
    if not arguments:
        return NO_ARGUMENTS
    return ", ".join(f"{key}={value}" for key, value in sorted(arguments.items()))


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
