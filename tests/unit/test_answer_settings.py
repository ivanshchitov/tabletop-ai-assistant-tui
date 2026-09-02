"""Валидация и неизменяемость AnswerSettings."""

import pytest

from core import config
from core.answer_settings import AnswerFormat, AnswerSettings, AnswerSettingsError


def test_defaults_match_config():
    settings = AnswerSettings()
    assert settings.max_words == config.DEFAULT_MAX_WORDS
    assert settings.list_limit == config.DEFAULT_LIST_LIMIT
    assert settings.format == AnswerFormat.FREE


@pytest.mark.parametrize("value", [config.MIN_MAX_WORDS, 200, config.MAX_MAX_WORDS])
def test_with_max_words_accepts_range_including_edges(value):
    assert AnswerSettings().with_max_words(value).max_words == value


@pytest.mark.parametrize(
    "value", [config.MIN_MAX_WORDS - 1, config.MAX_MAX_WORDS + 1, 0, -5, 10_000]
)
def test_with_max_words_rejects_out_of_range(value):
    with pytest.raises(AnswerSettingsError):
        AnswerSettings().with_max_words(value)


@pytest.mark.parametrize("value", [config.MIN_LIST_LIMIT, 3, config.MAX_LIST_LIMIT])
def test_with_list_limit_accepts_range_including_edges(value):
    assert AnswerSettings().with_list_limit(value).list_limit == value


@pytest.mark.parametrize(
    "value", [config.MIN_LIST_LIMIT - 1, config.MAX_LIST_LIMIT + 1, -1, 999]
)
def test_with_list_limit_rejects_out_of_range(value):
    with pytest.raises(AnswerSettingsError):
        AnswerSettings().with_list_limit(value)


def test_rejected_value_leaves_original_untouched():
    """Отказ вместо тихого клампинга: прежнее значение должно уцелеть."""
    settings = AnswerSettings().with_max_words(120)
    with pytest.raises(AnswerSettingsError):
        settings.with_max_words(9999)
    assert settings.max_words == 120


@pytest.mark.parametrize("fmt", list(AnswerFormat))
def test_with_format_accepts_every_enum_member(fmt):
    assert AnswerSettings().with_format(fmt).format == fmt


def test_with_methods_return_new_instance():
    original = AnswerSettings()
    assert original.with_max_words(50) is not original
    assert original.max_words == config.DEFAULT_MAX_WORDS


def test_with_max_words_preserves_other_fields():
    settings = AnswerSettings(max_words=100, format=AnswerFormat.JSON, list_limit=7)
    updated = settings.with_max_words(300)
    assert (updated.max_words, updated.format, updated.list_limit) == (300, AnswerFormat.JSON, 7)


def test_with_format_preserves_other_fields():
    settings = AnswerSettings(max_words=100, format=AnswerFormat.JSON, list_limit=7)
    updated = settings.with_format(AnswerFormat.COMPACT)
    assert (updated.max_words, updated.format, updated.list_limit) == (
        100,
        AnswerFormat.COMPACT,
        7,
    )


def test_with_list_limit_preserves_other_fields():
    settings = AnswerSettings(max_words=100, format=AnswerFormat.JSON, list_limit=7)
    updated = settings.with_list_limit(2)
    assert (updated.max_words, updated.format, updated.list_limit) == (
        100,
        AnswerFormat.JSON,
        2,
    )


def test_with_methods_chain():
    settings = (
        AnswerSettings()
        .with_max_words(42)
        .with_format(AnswerFormat.COMPACT)
        .with_list_limit(5)
    )
    assert (settings.max_words, settings.format, settings.list_limit) == (
        42,
        AnswerFormat.COMPACT,
        5,
    )


def test_answer_settings_error_is_value_error():
    """Тип ошибки — часть контракта: вызывающий код ловит её как ValueError."""
    assert issubclass(AnswerSettingsError, ValueError)


def test_answer_format_values_are_stable_strings():
    """Значения enum попадают в имена файлов ассетов, менять их нельзя молча."""
    assert {f.value for f in AnswerFormat} == {"compact", "json", "free"}
