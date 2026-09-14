"""Долговременная память агента (сведения о пользователе между сессиями) — отдельный файл.

Слой живёт дольше диалога: `/clear` его не касается, поэтому хранить его в конверте истории нельзя.
Записи — плоская таблица «ключ → значение с меткой категории»; ключи заданы правилами маршрутизации
(`core/memory_layers`) и командами пользователя, вытеснение не нужно.
"""

import json
from pathlib import Path
from typing import Dict, Optional, Tuple

from . import config, memory_layers


def _read_entries(raw: object) -> Dict[str, Tuple[str, str]]:
    """Разбор таблицы записей: ключ → (значение, категория).

    Запись без категории читается как заметка — пользователь мог дописать её в файл руками, и
    терять её из-за отсутствующего поля неправильно.
    """
    if not isinstance(raw, dict):
        return {}
    entries: Dict[str, Tuple[str, str]] = {}
    for key, value in raw.items():
        if isinstance(value, dict):
            text = value.get("value")
            category = value.get("category")
        else:
            text, category = value, None
        if not isinstance(text, (str, int, float, bool)):
            continue
        entries[str(key)] = (
            str(text),
            str(category) if isinstance(category, str) and category else memory_layers.CATEGORY_NOTE,
        )
    return entries


class LongTermMemory:
    """Файл долговременной памяти: чтение при создании, запись сразу после изменения."""

    def __init__(self, path: Path = config.MEMORY_FILE):
        self.path = path
        self._entries: Dict[str, Tuple[str, str]] = self._load()

    def _load(self) -> Dict[str, Tuple[str, str]]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return _read_entries(data.get("entries"))

    def records(self) -> Tuple[memory_layers.MemoryRecord, ...]:
        """Записи слоя по порядку появления: слой, категория, ключ и значение."""
        return tuple(
            memory_layers.MemoryRecord(
                layer=memory_layers.LONG_TERM, category=category, key=key, value=value
            )
            for key, (value, category) in self._entries.items()
        )

    def get(self, key: str) -> Optional[str]:
        entry = self._entries.get(key)
        return entry[0] if entry is not None else None

    def remember(self, key: str, value: str, category: str = memory_layers.CATEGORY_NOTE) -> None:
        """Записывает сведение: повторная запись по ключу заменяет значение, а не добавляет запись."""
        self._entries[key] = (value, category)
        self.save()

    def forget(self, key: str) -> bool:
        """Удаляет запись по ключу; False — такой записи нет."""
        if key not in self._entries:
            return False
        del self._entries[key]
        self.save()
        return True

    def clear(self) -> None:
        self._entries = {}
        self.save()

    def save(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "entries": {
                            key: {"value": value, "category": category}
                            for key, (value, category) in self._entries.items()
                        }
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError:
            pass
