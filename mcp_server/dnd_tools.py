"""Инструменты собственного MCP-сервера: объявление, схемы входа и выполнение вызова.

Разделение с `dnd_server.py` намеренное: здесь нет ни протокола, ни асинхронности, поэтому
каждый инструмент проверяется обычным юнит-тестом против локальной заглушки API — так же,
как панели интерфейса проверяются редьюсерами без терминала.

Три инструмента покрывают весь справочник, потому что все его разделы устроены одинаково:
список раздела и запись по идентификатору. Инструмент на раздел дал бы два десятка почти
одинаковых описаний, которые грузятся в каждый запрос выбора инструмента и при этом хуже
различаются моделью, — раздел дешевле передать параметром.

Ошибка — это текст результата, а не исключение: его читает модель, которая и выбирала
аргументы, поэтому в тексте перечисляются допустимые значения.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .dnd_api import RULESETS, DEFAULT_RULESET, DndAPI, DndAPIError

MIN_LIMIT = 1
MAX_LIMIT = 10
DEFAULT_LIMIT = 5

_RULESET_PARAMETER = {
    "type": "string",
    "description": "Редакция правил: 2014 (по умолчанию) или 2024.",
    "enum": list(RULESETS),
    "default": DEFAULT_RULESET,
}
_SECTION_PARAMETER = {
    "type": "string",
    "description": "Раздел справочника, например из ответа инструмента dnd_sections.",
}


@dataclass(frozen=True)
class ToolSpec:
    """Инструмент так, как он объявляется протоколом."""

    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)


TOOLS: Tuple[ToolSpec, ...] = (
    ToolSpec(
        name="dnd_sections",
        description=(
            "Перечень разделов справочника правил D&D 5e: заклинания, монстры, классы, "
            "снаряжение, состояния и прочие. Возвращает имена разделов для двух других "
            "инструментов."
        ),
        input_schema={
            "type": "object",
            "properties": {"ruleset": _RULESET_PARAMETER},
            "required": [],
        },
    ),
    ToolSpec(
        name="dnd_search",
        description=(
            "Поиск записей внутри раздела справочника по части названия: возвращает "
            "идентификаторы и названия найденных записей."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "section": _SECTION_PARAMETER,
                "query": {
                    "type": "string",
                    "description": "Часть названия записи, например «gob» или «fire».",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Сколько записей вернуть, от {MIN_LIMIT} до {MAX_LIMIT}.",
                    "default": DEFAULT_LIMIT,
                },
                "ruleset": _RULESET_PARAMETER,
            },
            "required": ["section", "query"],
        },
    ),
    ToolSpec(
        name="dnd_entry",
        description=(
            "Полная запись раздела справочника по идентификатору: например, характеристики "
            "монстра, описание заклинания или параметры класса."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "section": _SECTION_PARAMETER,
                "index": {
                    "type": "string",
                    "description": "Идентификатор записи, например «goblin» или «fireball».",
                },
                "ruleset": _RULESET_PARAMETER,
            },
            "required": ["section", "index"],
        },
    ),
)

TOOL_NAMES = tuple(tool.name for tool in TOOLS)


class ArgumentError(Exception):
    """Аргумент не удовлетворяет схеме: до внешнего API дело не доходит."""


class DndTools:
    """Выполнение вызовов инструментов поверх одного клиента внешнего API."""

    def __init__(self, api: Optional[DndAPI] = None) -> None:
        self.api = api if api is not None else DndAPI()
        # Перечень разделов кэшируется: он нужен и как ответ инструмента, и как проверка
        # аргумента `section`, а меняется на стороне сервиса разве что с новой редакцией.
        self._sections: Dict[str, List[str]] = {}

    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        """Выполнить инструмент и вернуть текст результата — включая текст отказа."""
        handlers = {
            "dnd_sections": self._sections_tool,
            "dnd_search": self._search_tool,
            "dnd_entry": self._entry_tool,
        }
        handler = handlers.get(name)
        if handler is None:
            return f"Инструмент «{name}» не объявлен. Доступны: {', '.join(TOOL_NAMES)}."
        try:
            return handler(arguments or {})
        except ArgumentError as error:
            return f"Неверные аргументы: {error}"
        except DndAPIError as error:
            return f"Источник данных не ответил: {error}"

    # --- инструменты ---

    def _sections_tool(self, arguments: Dict[str, Any]) -> str:
        ruleset = self._ruleset(arguments)
        sections = self._known_sections(ruleset)
        return "Разделы справочника (редакция {0}): {1}".format(ruleset, ", ".join(sections))

    def _search_tool(self, arguments: Dict[str, Any]) -> str:
        # Порядок важен: всё, что проверяется локально, проверяется до обращения к API —
        # перечень разделов для проверки `section` при холодном кэше стоит запроса.
        ruleset = self._ruleset(arguments)
        query = self._text(arguments, "query")
        limit = self._limit(arguments)
        section = self._section(arguments, ruleset)
        results = self.api.search(section, query, limit=limit, ruleset=ruleset)
        if not results:
            return f"В разделе «{section}» ничего не найдено по запросу «{query}»."
        lines = [f"Найдено в разделе «{section}» (редакция {ruleset}):"]
        lines += [f"- {item.get('index', '?')}: {item.get('name', '')}" for item in results]
        return "\n".join(lines)

    def _entry_tool(self, arguments: Dict[str, Any]) -> str:
        ruleset = self._ruleset(arguments)
        index = self._text(arguments, "index")
        section = self._section(arguments, ruleset)
        entry = self.api.entry(section, index, ruleset=ruleset)
        body = json.dumps(entry, ensure_ascii=False, indent=2)
        return f"Запись «{index}» раздела «{section}» (редакция {ruleset}):\n{body}"

    # --- разбор аргументов ---

    def _ruleset(self, arguments: Dict[str, Any]) -> str:
        value = str(arguments.get("ruleset") or DEFAULT_RULESET).strip()
        if value not in RULESETS:
            raise ArgumentError(
                f"ruleset «{value}» недопустим, возможные значения: {', '.join(RULESETS)}"
            )
        return value

    def _text(self, arguments: Dict[str, Any], key: str) -> str:
        value = str(arguments.get(key) or "").strip()
        if not value:
            raise ArgumentError(f"параметр {key} обязателен и не может быть пустым")
        return value

    def _limit(self, arguments: Dict[str, Any]) -> int:
        raw = arguments.get("limit", DEFAULT_LIMIT)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ArgumentError(f"limit должен быть целым числом, получено «{raw}»") from None
        if not MIN_LIMIT <= value <= MAX_LIMIT:
            raise ArgumentError(f"limit должен быть от {MIN_LIMIT} до {MAX_LIMIT}, получено {value}")
        return value

    def _section(self, arguments: Dict[str, Any], ruleset: str) -> str:
        section = self._text(arguments, "section")
        known = self._known_sections(ruleset)
        if section not in known:
            raise ArgumentError(
                f"раздел «{section}» неизвестен, доступны: {', '.join(known)}"
            )
        return section

    def _known_sections(self, ruleset: str) -> List[str]:
        if ruleset not in self._sections:
            self._sections[ruleset] = self.api.sections(ruleset)
        return self._sections[ruleset]
