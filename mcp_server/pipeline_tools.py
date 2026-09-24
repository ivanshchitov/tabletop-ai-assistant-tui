"""Инструменты обработки и сохранения собственного сервера: сводка и запись в файл (день 19).

Вместе с поиском (`dnd_search` с `details=true`) они образуют цепочку «получить данные →
обработать → сохранить»: агент выполняет шаги сам, подставляя результат прошлого шага в
аргумент `text` следующего. Поэтому оба инструмента принимают на вход обычный текст —
ровно то, что вернул предыдущий инструмент, — а не структуры, которые пришлось бы собирать.

Сводка детерминированная: у сервера нет доступа к языковой модели, а воспроизводимый
результат проверяется тестом. Отказы возвращаются с признаком неуспеха — сервер ставит по нему
протокольный признак ошибки, и цепочка не передаёт текст отказа следующему шагу.

Модуль отдельный, как и планировщик: эти инструменты не справочные и не ставятся в
расписание — сохранение файла без входных данных смысла не имеет.
"""

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import config

from .dnd_tools import ToolSpec

MIN_ITEMS = 1
MAX_ITEMS = 20
DEFAULT_ITEMS = 10
EXPORT_SUFFIX = ".md"
# Имя файла — одно звено пути: буквы, цифры, дефис, подчёркивание, точка. Разделители каталогов
# и «..» отсекаются этим же правилом, поэтому выйти за каталог выгрузок нельзя.
_FILENAME_RE = re.compile(r"^[\w.-]+$")
_PLAIN_ITEM_RE = re.compile(r"^\s*-\s*([^:]+):\s*(.+?)\s*$")
_SENTENCE_RE = re.compile(r"^(.+?[.!?])(\s|$)")

PIPELINE_TOOLS: Tuple[ToolSpec, ...] = (
    ToolSpec(
        name="dnd_summarize",
        description=(
            "Краткая сводка записей справочника D&D 5e: по строке на запись — название, "
            "ключевые поля и первое предложение описания. Вход — текст результата поиска "
            "(лучше dnd_search с details=true)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Текст с записями — результат другого инструмента.",
                },
                "max_items": {
                    "type": "integer",
                    "description": f"Сколько записей включить, от {MIN_ITEMS} до {MAX_ITEMS}.",
                    "default": DEFAULT_ITEMS,
                },
            },
            "required": ["text"],
        },
    ),
    ToolSpec(
        name="save_to_file",
        description=(
            "Сохранить текст в markdown-файл каталога выгрузок; существующий файл не "
            "перезаписывается. Возвращает путь к файлу и число сохранённых символов."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Что сохранить."},
                "filename": {
                    "type": "string",
                    "description": (
                        "Имя файла без каталогов, например «fire-spells»; расширение .md "
                        "добавляется само."
                    ),
                },
            },
            "required": ["text", "filename"],
        },
    ),
)

PIPELINE_TOOL_NAMES = tuple(tool.name for tool in PIPELINE_TOOLS)


class _Refused(Exception):
    """Вход не годится: текст отказа уходит вызывающей стороне с признаком ошибки."""


class PipelineTools:
    """Выполнение сводки и сохранения; каталог выгрузок читается в момент вызова."""

    def __init__(self, exports_dir: Optional[Path] = None) -> None:
        self._exports_dir = exports_dir

    def call(self, name: str, arguments: Dict[str, Any]) -> str:
        return self.call_result(name, arguments)[1]

    def call_result(self, name: str, arguments: Dict[str, Any]) -> Tuple[bool, str]:
        handlers = {"dnd_summarize": self._summarize, "save_to_file": self._save}
        handler = handlers.get(name)
        if handler is None:
            return False, f"Инструмент «{name}» не объявлен. Доступны: {', '.join(PIPELINE_TOOL_NAMES)}."
        try:
            return True, handler(arguments or {})
        except _Refused as error:
            return False, f"Неверные аргументы: {error}"
        except OSError as error:
            return False, f"Не удалось записать файл: {error}"

    # --- сводка ---

    def _summarize(self, arguments: Dict[str, Any]) -> str:
        text = _text(arguments, "text")
        limit = _items(arguments)
        records = _records(text)
        if not records:
            raise _Refused(
                "в тексте нет записей справочника — ожидается результат dnd_search "
                "(JSON-массив записей или строки «- идентификатор: название»)"
            )
        shown = records[:limit]
        lines = [f"Сводка: {len(records)} {_plural(len(records), 'запись', 'записи', 'записей')}"]
        lines += [f"- {_summary_line(record)}" for record in shown]
        if len(records) > len(shown):
            lines.append(f"…и ещё {len(records) - len(shown)}")
        return "\n".join(lines)

    # --- сохранение ---

    def _save(self, arguments: Dict[str, Any]) -> str:
        text = _text(arguments, "text")
        name = str(arguments.get("filename") or "").strip()
        if name.lower().endswith(EXPORT_SUFFIX):
            name = name[: -len(EXPORT_SUFFIX)]
        if not name or not _FILENAME_RE.match(name) or set(name) == {"."} or ".." in name:
            raise _Refused(
                f"имя файла «{name}» недопустимо: только буквы, цифры, «-», «_» и «.», "
                "без каталогов и «..»"
            )
        directory = self._exports_dir or config.exports_dir()
        directory.mkdir(parents=True, exist_ok=True)
        target = _free_path(directory, name)
        target.write_text(text, encoding="utf-8")
        return f"Сохранено в {target}: {len(text)} {_plural(len(text), 'символ', 'символа', 'символов')}"


def _text(arguments: Dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _Refused(f"параметр {key} обязателен и не может быть пустым")
    return value


def _items(arguments: Dict[str, Any]) -> int:
    raw = arguments.get("max_items", DEFAULT_ITEMS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _Refused(f"max_items должен быть целым числом, получено «{raw}»") from None
    if not MIN_ITEMS <= value <= MAX_ITEMS:
        raise _Refused(f"max_items должен быть от {MIN_ITEMS} до {MAX_ITEMS}, получено {value}")
    return value


def _records(text: str) -> List[Dict[str, Any]]:
    """Записи из текста: JSON-массив (или объект с results) после заголовка, иначе пункты списка."""
    start = min((i for i in (text.find("["), text.find("{")) if i >= 0), default=-1)
    if start >= 0:
        try:
            data = json.loads(text[start:])
        except ValueError:
            data = None
        if isinstance(data, dict):
            data = data.get("results")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict) and item.get("name")]
    records = []
    for line in text.splitlines():
        match = _PLAIN_ITEM_RE.match(line)
        if match:
            records.append({"index": match.group(1).strip(), "name": match.group(2)})
    return records


def _summary_line(record: Dict[str, Any]) -> str:
    facts: List[str] = []
    level = record.get("level")
    if level is not None:
        facts.append("заговор" if level == 0 else f"уровень {level}")
    for key in ("school", "type", "size", "range", "casting_time"):
        if record.get(key):
            facts.append(str(record[key]))
    if record.get("challenge_rating") is not None:
        facts.append(f"опасность {record['challenge_rating']}")
    if record.get("hit_points") is not None:
        facts.append(f"хиты {record['hit_points']}")
    if record.get("hit_die") is not None:
        facts.append(f"кость хитов d{record['hit_die']}")
    line = str(record["name"])
    if facts:
        line += f" ({', '.join(facts)})"
    desc = str(record.get("desc") or "").strip()
    if desc:
        match = _SENTENCE_RE.match(desc)
        line += " — " + (match.group(1) if match else desc)
    return line


def _free_path(directory: Path, name: str) -> Path:
    """Путь, который ещё не занят: существующий файл не перезаписывается."""
    target = directory / f"{name}{EXPORT_SUFFIX}"
    number = 2
    while target.exists():
        target = directory / f"{name}-{number}{EXPORT_SUFFIX}"
        number += 1
    return target


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many
