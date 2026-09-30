"""Чистая часть второго этапа RAG: настройки, ответы модели и отбор источников."""

import json
import math
from dataclasses import dataclass, replace
from enum import Enum
from typing import Optional, Sequence, Tuple

from .rules_index import SearchResult


class RetrievalError(ValueError):
    """Непригодная настройка или ответ вспомогательной модели."""


class RetrievalMode(str, Enum):
    BASELINE = "baseline"
    ENHANCED = "enhanced"


def _valid_score(value: object) -> bool:
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


@dataclass(frozen=True)
class RetrievalSettings:
    mode: RetrievalMode = RetrievalMode.ENHANCED
    before: int = 20
    after: int = 3
    threshold: float = 0.6

    def __post_init__(self) -> None:
        try:
            mode = RetrievalMode(self.mode)
        except (ValueError, TypeError) as exc:
            raise RetrievalError("Режим поиска: baseline или enhanced.") from exc
        if type(self.before) is not int or not 1 <= self.before <= 30:
            raise RetrievalError("before: целое число от 1 до 30.")
        if type(self.after) is not int or not 1 <= self.after <= 10:
            raise RetrievalError("after: целое число от 1 до 10.")
        if self.after > self.before:
            raise RetrievalError("after не может быть больше before.")
        if not _valid_score(self.threshold):
            raise RetrievalError("threshold: конечное число от 0 до 1.")
        object.__setattr__(self, "mode", mode)


@dataclass(frozen=True)
class Relevance:
    id: int
    score: float
    reason: str


@dataclass(frozen=True)
class CandidateDecision:
    result: SearchResult
    relevance: Optional[Relevance]
    disposition: str


@dataclass(frozen=True)
class Selection:
    # Решения сохраняют порядок первого этапа, результаты — порядок реранкера.
    decisions: Tuple[CandidateDecision, ...] = ()
    results: Tuple[SearchResult, ...] = ()


@dataclass(frozen=True)
class RetrievalReport:
    question: str
    query: str
    settings: RetrievalSettings
    status: str
    selection: Selection = Selection()
    error: Optional[str] = None


def parse_tuning(settings: RetrievalSettings, text: str) -> RetrievalSettings:
    """Проверяет все параметры до создания нового экземпляра, не меняя прежний."""
    changes = {}
    for token in text.split():
        key, separator, value = token.partition("=")
        if not separator or key not in ("before", "after", "threshold"):
            raise RetrievalError("Параметры: before=<K> after=<K> threshold=<T>.")
        if key in changes:
            raise RetrievalError("Параметр {} указан повторно.".format(key))
        try:
            changes[key] = float(value) if key == "threshold" else int(value)
        except ValueError as exc:
            raise RetrievalError("{}: неверное число.".format(key)) from exc
    if not changes:
        raise RetrievalError("Укажите before=<K>, after=<K> или threshold=<T>.")
    return replace(settings, **changes)


def _first_json_object(text: str) -> dict:
    """Первый JSON-объект, в том числе внутри пояснений или блока кода."""
    start = text.find("{")
    if start < 0:
        raise RetrievalError("Ответ модели не содержит JSON-объект.")
    try:
        data, _end = json.JSONDecoder().raw_decode(text, start)
    except ValueError as exc:
        raise RetrievalError("Ответ модели содержит непригодный JSON.") from exc
    return data


def parse_query(text: str) -> str:
    data = _first_json_object(text)
    query = data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise RetrievalError("Переписывание не вернуло непустую строку query.")
    return query.strip()


def parse_ratings(text: str, candidate_count: int) -> Tuple[Relevance, ...]:
    """Принимает только полный набор корректных оценок; частичного успеха нет."""
    data = _first_json_object(text)
    rows = data.get("results")
    if not isinstance(rows, list) or len(rows) != candidate_count:
        raise RetrievalError("Отбор должен оценить каждый из {} кандидатов.".format(candidate_count))
    ratings = {}
    for row in rows:
        if not isinstance(row, dict):
            raise RetrievalError("Оценка кандидата должна быть JSON-объектом.")
        number = row.get("id")
        if type(number) is not int or not 1 <= number <= candidate_count:
            raise RetrievalError("Отбор вернул неизвестный id кандидата.")
        if number in ratings:
            raise RetrievalError("Отбор повторил id кандидата {}.".format(number))
        score = row.get("score")
        reason = row.get("reason")
        if not _valid_score(score):
            raise RetrievalError("Кандидат {}: score должен быть конечным числом от 0 до 1.".format(number))
        if not isinstance(reason, str):
            raise RetrievalError("Кандидат {}: reason должен быть строкой.".format(number))
        ratings[number] = Relevance(number, score, reason)
    # Полное покрытие обеспечивают размер списка, диапазон id и отсутствие дублей.
    return tuple(ratings[number] for number in range(1, candidate_count + 1))


def select_candidates(
    candidates: Sequence[SearchResult],
    ratings: Sequence[Relevance],
    settings: RetrievalSettings,
) -> Selection:
    """Стабильный отбор по порогу и top-K, без изменения текстов и cosine-оценок."""
    if len(candidates) > settings.before:
        raise RetrievalError("Получено больше кандидатов, чем разрешает before.")
    if len(ratings) != len(candidates) or any(
        rating.id != number for number, rating in enumerate(ratings, 1)
    ):
        raise RetrievalError("Для отбора нужен полный набор оценок в порядке кандидатов.")
    ranked = sorted(
        (index for index, rating in enumerate(ratings) if rating.score >= settings.threshold),
        key=lambda index: ratings[index].score,
        reverse=True,
    )[:settings.after]
    selected = set(ranked)
    decisions = tuple(
        CandidateDecision(
            result,
            ratings[index],
            "below_threshold" if ratings[index].score < settings.threshold
            else "selected" if index in selected else "top_k",
        )
        for index, result in enumerate(candidates)
    )
    return Selection(decisions, tuple(candidates[index] for index in ranked))
