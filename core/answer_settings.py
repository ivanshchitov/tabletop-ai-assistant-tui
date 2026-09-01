"""Настройки формата и объёма ответа (общие для сессии TUI)."""

from dataclasses import dataclass
from enum import Enum

from . import config


class AnswerFormat(str, Enum):
    """Формат ответа, выбираемый пользователем через /format."""

    COMPACT = "compact"
    JSON = "json"
    FREE = "free"


class AnswerSettingsError(ValueError):
    """Невалидное значение настройки (без тихого клампинга)."""


@dataclass
class AnswerSettings:
    max_words: int = config.DEFAULT_MAX_WORDS
    format: AnswerFormat = AnswerFormat.FREE
    list_limit: int = config.DEFAULT_LIST_LIMIT

    def with_max_words(self, value: int) -> "AnswerSettings":
        if not config.MIN_MAX_WORDS <= value <= config.MAX_MAX_WORDS:
            raise AnswerSettingsError(
                f"Значение должно быть в диапазоне {config.MIN_MAX_WORDS}..{config.MAX_MAX_WORDS}."
            )
        return AnswerSettings(max_words=value, format=self.format, list_limit=self.list_limit)

    def with_format(self, value: AnswerFormat) -> "AnswerSettings":
        return AnswerSettings(max_words=self.max_words, format=value, list_limit=self.list_limit)

    def with_list_limit(self, value: int) -> "AnswerSettings":
        if not config.MIN_LIST_LIMIT <= value <= config.MAX_LIST_LIMIT:
            raise AnswerSettingsError(
                f"Значение должно быть в диапазоне {config.MIN_LIST_LIMIT}..{config.MAX_LIST_LIMIT}."
            )
        return AnswerSettings(max_words=self.max_words, format=self.format, list_limit=value)
