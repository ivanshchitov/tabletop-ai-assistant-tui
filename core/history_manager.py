"""Сохранение и загрузка истории диалогов (с метриками, сжатым резюме и блоком фактов).

Файл — конверт: {"summary", "summary_covers", "facts", "dialogues"}. Резюме и факты — производная
память агента (дайджест ведущих обменов и словарь важных сведений диалога); исходные записи
хранятся полностью, вытеснения нет. Файл прежнего вида (голый список записей) читается как
конверт без резюме и фактов.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config
from .usage import SessionUsage, sum_usage


def _read_facts(raw: Any) -> Dict[str, str]:
    """Разбор блока фактов из конверта: только плоские пары «строка — строка»."""
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if isinstance(value, (str, int, float, bool))
    }


class HistoryManager:
    def __init__(self, path: Path = config.HISTORY_FILE):
        self.path = path
        self.summary: Optional[str] = None
        self.summary_covers: int = 0
        self.facts: Dict[str, str] = {}
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
            # Старый формат — голый список записей: конверт без резюме и фактов.
            return data
        if isinstance(data, dict):
            summary = data.get("summary")
            self.summary = summary if isinstance(summary, str) else None
            try:
                self.summary_covers = int(data.get("summary_covers", 0))
            except (TypeError, ValueError):
                self.summary_covers = 0
            self.facts = _read_facts(data.get("facts"))
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

    def set_facts(self, facts: Dict[str, str]) -> None:
        """Обновляет блок фактов диалога; файл сразу переписывается."""
        self.facts = dict(facts)
        self.save()

    def clear(self) -> None:
        self.summary = None
        self.summary_covers = 0
        self.facts = {}
        self.dialogues = []
        self.save()

    def save(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "summary": self.summary,
                        "summary_covers": self.summary_covers,
                        "facts": self.facts,
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
