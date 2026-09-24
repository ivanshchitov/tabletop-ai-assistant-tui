"""Инструменты собственного MCP-сервера: объявление, схемы входа и выполнение вызова.

Разделение с `dnd_server.py` намеренное: здесь нет ни протокола, ни асинхронности, поэтому
каждый инструмент проверяется обычным юнит-тестом против локальной заглушки API — так же,
как панели интерфейса проверяются редьюсерами без терминала.

Справочные инструменты покрывают весь справочник, потому что все его разделы устроены
одинаково: список раздела и запись по идентификатору. Инструмент на раздел дал бы два десятка
почти одинаковых описаний, которые грузятся в каждый запрос выбора инструмента и при этом хуже
различаются моделью, — раздел дешевле передать параметром.

Сбор данных (`dnd_digest`) отличается от поиска тем, что помнит прошлые вызовы: собранное
складывается в хранилище планировщика, поэтому повторный вызов отличает записи, встреченные
впервые, от уже известных. Это и делает его пригодным для расписания — по одному вызову в
период, с накоплением картины.

Ошибка — это текст результата, а не исключение: его читает модель, которая и выбирала
аргументы, поэтому в тексте перечисляются допустимые значения.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from core.schedule_store import ScheduleStore

from .dnd_api import RULESETS, DEFAULT_RULESET, DndAPI, DndAPIError

MIN_LIMIT = 1
MAX_LIMIT = 10
DEFAULT_LIMIT = 5
# Сбор берёт раздел целиком, а не первые пять записей: его смысл — накопленная картина,
# поэтому потолок отдельный и заметно выше лимита поиска.
MAX_DIGEST_LIMIT = 50
DEFAULT_DIGEST_LIMIT = 20

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
            "идентификаторы и названия найденных записей, а с details=true — их основные поля."
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
                "details": {
                    "type": "boolean",
                    "description": (
                        "true — вернуть основные поля каждой найденной записи JSON-массивом "
                        "(вход для сводки dnd_summarize), false — только идентификаторы и названия."
                    ),
                    "default": False,
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
    ToolSpec(
        name="dnd_digest",
        description=(
            "Сбор записей раздела справочника с накоплением: возвращает срез раздела и "
            "отмечает записи, встреченные впервые по сравнению с прошлыми сборами. "
            "Подходит для регулярного запуска по расписанию."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "section": _SECTION_PARAMETER,
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Сколько записей собрать за один раз, от {MIN_LIMIT} до {MAX_DIGEST_LIMIT}."
                    ),
                    "default": DEFAULT_DIGEST_LIMIT,
                },
                "ruleset": _RULESET_PARAMETER,
            },
            "required": ["section"],
        },
    ),
)

TOOL_NAMES = tuple(tool.name for tool in TOOLS)


# Поля, которые подробный поиск берёт из записи: по ним сводка строит строку записи. Берутся те,
# что есть, — у разделов разный набор, а полная запись раздула бы каждый запрос к модели.
BRIEF_FIELDS = (
    "level", "school", "casting_time", "range", "duration",
    "size", "type", "challenge_rating", "hit_points", "hit_die",
)
MAX_BRIEF_DESC_CHARS = 300


def brief_record(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Основные поля записи: идентификатор, название, известные поля и начало описания."""
    record: Dict[str, Any] = {"index": entry.get("index", ""), "name": entry.get("name", "")}
    for key in BRIEF_FIELDS:
        value = entry.get(key)
        if isinstance(value, dict):
            value = value.get("name")
        if value is not None and value != "":
            record[key] = value
    desc = entry.get("desc")
    if isinstance(desc, list):
        desc = " ".join(str(part) for part in desc)
    if isinstance(desc, str) and desc.strip():
        record["desc"] = desc.strip()[:MAX_BRIEF_DESC_CHARS]
    return record


class ArgumentError(Exception):
    """Аргумент не удовлетворяет схеме: до внешнего API дело не доходит."""


class DndTools:
    """Выполнение вызовов инструментов поверх одного клиента внешнего API."""

    def __init__(self, api: Optional[DndAPI] = None, store: Optional[ScheduleStore] = None) -> None:
        self.api = api if api is not None else DndAPI()
        # Хранилище нужно сбору: накопленное переживает и вызов, и перезапуск процесса.
        self.store = store if store is not None else ScheduleStore()
        # Перечень разделов кэшируется: он нужен и как ответ инструмента, и как проверка
        # аргумента `section`, а меняется на стороне сервиса разве что с новой редакцией.
        self._sections: Dict[str, List[str]] = {}

    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        """Выполнить инструмент и вернуть текст результата — включая текст отказа."""
        return self.call_result(name, arguments)[1]

    def call_result(self, name: str, arguments: Dict[str, Any]) -> Tuple[bool, str]:
        """То же, но с признаком успеха: планировщику нужно знать исход, а не разбирать текст."""
        handlers = {
            "dnd_sections": self._sections_tool,
            "dnd_search": self._search_tool,
            "dnd_entry": self._entry_tool,
            "dnd_digest": self._digest_tool,
        }
        handler = handlers.get(name)
        if handler is None:
            return False, f"Инструмент «{name}» не объявлен. Доступны: {', '.join(TOOL_NAMES)}."
        try:
            return True, handler(arguments or {})
        except ArgumentError as error:
            return False, f"Неверные аргументы: {error}"
        except DndAPIError as error:
            return False, f"Источник данных не ответил: {error}"

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
        if self._flag(arguments, "details"):
            records = [
                brief_record(self.api.entry(section, str(item.get("index", "")), ruleset=ruleset))
                for item in results
            ]
            header = f"Найдено в разделе «{section}» (редакция {ruleset}), записей: {len(records)}"
            return header + "\n" + json.dumps(records, ensure_ascii=False, indent=2)
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

    def _digest_tool(self, arguments: Dict[str, Any]) -> str:
        ruleset = self._ruleset(arguments)
        limit = self._limit(arguments, default=DEFAULT_DIGEST_LIMIT, maximum=MAX_DIGEST_LIMIT)
        section = self._section(arguments, ruleset)
        results = self.api.search(section, limit=limit, ruleset=ruleset)
        names = [str(item.get("name") or item.get("index") or "?") for item in results]
        # Ключ накопления включает ветку правил: одна и та же запись в редакциях 2014 и 2024 —
        # разные данные, и «встречено впервые» должно считаться по каждой ветке отдельно.
        fresh = self.store.remember_collected(f"{section}/{ruleset}", names)
        lines = [
            f"Сводка раздела «{section}» (редакция {ruleset}): собрано {len(names)}, "
            f"впервые: {len(fresh)}"
        ]
        if fresh:
            lines += [f"- {name}" for name in fresh]
        return "\n".join(lines)

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

    def _flag(self, arguments: Dict[str, Any], key: str) -> bool:
        # Ручной вызов разбирает ввод парами «ключ=значение», поэтому булево приходит строкой.
        value = arguments.get(key, False)
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "да"}
        return bool(value)

    def _limit(
        self, arguments: Dict[str, Any], default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT
    ) -> int:
        raw = arguments.get("limit", default)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise ArgumentError(f"limit должен быть целым числом, получено «{raw}»") from None
        if not MIN_LIMIT <= value <= maximum:
            raise ArgumentError(f"limit должен быть от {MIN_LIMIT} до {maximum}, получено {value}")
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
