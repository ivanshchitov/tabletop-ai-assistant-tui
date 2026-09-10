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
    temperature: float = config.TEMPERATURE
    compress_after: int = config.DEFAULT_COMPRESS_AFTER
    max_session_tokens: int = config.DEFAULT_MAX_SESSION_TOKENS

    def with_max_words(self, value: int) -> "AnswerSettings":
        if not config.MIN_MAX_WORDS <= value <= config.MAX_MAX_WORDS:
            raise AnswerSettingsError(
                f"Значение должно быть в диапазоне {config.MIN_MAX_WORDS}..{config.MAX_MAX_WORDS}."
            )
        return AnswerSettings(
            max_words=value,
            format=self.format,
            list_limit=self.list_limit,
            temperature=self.temperature,
            compress_after=self.compress_after,
            max_session_tokens=self.max_session_tokens,
        )

    def with_format(self, value: AnswerFormat) -> "AnswerSettings":
        return AnswerSettings(
            max_words=self.max_words,
            format=value,
            list_limit=self.list_limit,
            temperature=self.temperature,
            compress_after=self.compress_after,
            max_session_tokens=self.max_session_tokens,
        )

    def with_list_limit(self, value: int) -> "AnswerSettings":
        if not config.MIN_LIST_LIMIT <= value <= config.MAX_LIST_LIMIT:
            raise AnswerSettingsError(
                f"Значение должно быть в диапазоне {config.MIN_LIST_LIMIT}..{config.MAX_LIST_LIMIT}."
            )
        return AnswerSettings(
            max_words=self.max_words,
            format=self.format,
            list_limit=value,
            temperature=self.temperature,
            compress_after=self.compress_after,
            max_session_tokens=self.max_session_tokens,
        )

    def with_temperature(self, value: float) -> "AnswerSettings":
        if not config.MIN_TEMPERATURE <= value <= config.MAX_TEMPERATURE:
            raise AnswerSettingsError(
                f"Температура должна быть в диапазоне "
                f"{config.MIN_TEMPERATURE}..{config.MAX_TEMPERATURE}."
            )
        # Пользовательский ввод ограничен одним знаком после точки ещё на экране
        # настроек; здесь та же проверка служит инвариантом для программных вызовов.
        if round(value, 1) != value:
            raise AnswerSettingsError(
                "Температура задаётся числом не более чем с одним знаком после точки."
            )
        return AnswerSettings(
            max_words=self.max_words,
            format=self.format,
            list_limit=self.list_limit,
            temperature=value,
            compress_after=self.compress_after,
            max_session_tokens=self.max_session_tokens,
        )

    def with_compress_after(self, value: int) -> "AnswerSettings":
        if not config.MIN_COMPRESS_AFTER <= value <= config.MAX_COMPRESS_AFTER:
            raise AnswerSettingsError(
                f"Порог сжатия должен быть в диапазоне "
                f"{config.MIN_COMPRESS_AFTER}..{config.MAX_COMPRESS_AFTER}."
            )
        # Пользовательский ввод ограничен целыми ещё на экране настроек; здесь та же
        # проверка служит инвариантом для программных вызовов.
        if round(value) != value:
            raise AnswerSettingsError("Порог сжатия задаётся целым числом сообщений.")
        return AnswerSettings(
            max_words=self.max_words,
            format=self.format,
            list_limit=self.list_limit,
            temperature=self.temperature,
            compress_after=value,
            max_session_tokens=self.max_session_tokens,
        )

    def with_max_session_tokens(self, value: int) -> "AnswerSettings":
        if not config.MIN_MAX_SESSION_TOKENS <= value <= config.MAX_MAX_SESSION_TOKENS:
            raise AnswerSettingsError(
                f"Потолок контекста должен быть в диапазоне "
                f"{config.MIN_MAX_SESSION_TOKENS}..{config.MAX_MAX_SESSION_TOKENS}."
            )
        if round(value) != value:
            raise AnswerSettingsError("Потолок контекста задаётся целым числом токенов.")
        return AnswerSettings(
            max_words=self.max_words,
            format=self.format,
            list_limit=self.list_limit,
            temperature=self.temperature,
            compress_after=self.compress_after,
            max_session_tokens=value,
        )
