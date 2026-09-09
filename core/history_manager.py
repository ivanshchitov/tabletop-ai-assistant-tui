"""Сохранение и загрузка истории диалогов (с метриками использования каждого обмена)."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .usage import SessionUsage, sum_usage



class HistoryManager:
    def __init__(self, path: Path = config.HISTORY_FILE, limit: int = config.HISTORY_LIMIT):
        self.path = path
        self.limit = limit
        self.dialogues: List[Dict[str, str]] = self._load()

    def _load(self) -> List[Dict[str, str]]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        if isinstance(data, list):
            return data[-self.limit :]
        return []

    def add(
        self,
        question: str,
        answer: str,
        usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Дописывает обмен; usage — метрики запроса (None даёт запись прежнего вида)."""
        record: Dict[str, Any] = {"question": question, "answer": answer}
        if usage is not None:
            record["usage"] = usage
        self.dialogues.append(record)
        self.dialogues = self.dialogues[-self.limit :]
        self.save()

    def clear(self) -> None:
        self.dialogues = []
        self.save()

    def save(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(self.dialogues, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def count(self) -> int:
        return len(self.dialogues)

    def total_usage(self) -> SessionUsage:
        """Расход всей сохранённой истории: сумма метрик по записям файла.

        Вытесненные за лимит записи уносят свой расход — итог отражает удержанную
        историю (см. core/usage.sum_usage).
        """
        return sum_usage(self.dialogues)
