"""Сохранение и загрузка истории диалогов (с метриками и сжатым резюме агента).

Файл — конверт: {"summary", "summary_covers", "dialogues"}. Резюме — производная память
агента (дайджест ведущих обменов); исходные записи хранятся полностью, вытеснения нет.
Файл прежнего вида (голый список записей) читается как конверт без резюме.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .usage import SessionUsage, sum_usage


class HistoryManager:
    def __init__(self, path: Path = config.HISTORY_FILE):
        self.path = path
        self.summary: Optional[str] = None
        self.summary_covers: int = 0
        self.dialogues: List[Dict[str, Any]] = self._load()

    def _load(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        if isinstance(data, list):
            # Старый формат — голый список записей: конверт без резюме.
            return data
        if isinstance(data, dict):
            summary = data.get("summary")
            self.summary = summary if isinstance(summary, str) else None
            try:
                self.summary_covers = int(data.get("summary_covers", 0))
            except (TypeError, ValueError):
                self.summary_covers = 0
            records = data.get("dialogues", [])
            return records if isinstance(records, list) else []
        return []

    def add(
        self,
        question: str,
        answer: str,
        usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Дописывает обмен без вытеснения прежних; usage — метрики запроса."""
        record: Dict[str, Any] = {"question": question, "answer": answer}
        if usage is not None:
            record["usage"] = usage
        self.dialogues.append(record)
        self.save()

    def set_summary(self, summary: str, summary_covers: int) -> None:
        """Обновляет сжатое резюме и число покрытых им ведущих обменов; файл сразу переписывается."""
        self.summary = summary
        self.summary_covers = summary_covers
        self.save()

    def clear(self) -> None:
        self.summary = None
        self.summary_covers = 0
        self.dialogues = []
        self.save()

    def save(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "summary": self.summary,
                        "summary_covers": self.summary_covers,
                        "dialogues": self.dialogues,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError:
            pass

    def count(self) -> int:
        return len(self.dialogues)

    def total_usage(self) -> SessionUsage:
        """Расход всей сохранённой истории: сумма метрик по записям файла.

        Записей в файле теперь неограниченно — итог честный расход всего диалога.
        """
        return sum_usage(self.dialogues)
