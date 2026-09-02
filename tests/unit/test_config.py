"""Конфигурация: пересчёт токенов, переключатели окружения, доступ к ключу."""

import importlib

import pytest

from core import config


def test_max_tokens_formula():
    assert config.max_tokens_for_words(100) == 100 * config.WORDS_TO_TOKENS_RATIO + config.TOKENS_OVERHEAD


def test_max_tokens_is_monotonic():
    values = [config.max_tokens_for_words(w) for w in (10, 50, 200, 500)]
    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_max_tokens_leaves_headroom_above_word_limit():
    """max_tokens — технический потолок с запасом, а не сам лимит длины ответа."""
    for words in (config.MIN_MAX_WORDS, config.DEFAULT_MAX_WORDS, config.MAX_MAX_WORDS):
        assert config.max_tokens_for_words(words) > words


def test_ranges_are_sane():
    assert config.MIN_MAX_WORDS < config.DEFAULT_MAX_WORDS < config.MAX_MAX_WORDS
    assert config.MIN_LIST_LIMIT <= config.DEFAULT_LIST_LIMIT <= config.MAX_LIST_LIMIT


def test_get_api_key_reads_environment(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-from-env")
    assert config.get_api_key() == "sk-from-env"


def test_set_api_key_runtime_overrides(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    config.set_api_key_runtime("sk-runtime")
    assert config.get_api_key() == "sk-runtime"


def test_api_url_defaults_to_opencode_zen(monkeypatch):
    monkeypatch.delenv("OPENCODE_API_URL", raising=False)
    reloaded = importlib.reload(config)
    try:
        assert reloaded.API_URL == reloaded.DEFAULT_API_URL
        assert reloaded.API_URL.startswith("https://opencode.ai/zen/")
    finally:
        importlib.reload(config)


def test_api_url_override_from_environment(monkeypatch):
    """Переключатель, на котором держатся e2e-тесты против локального stub-сервера."""
    monkeypatch.setenv("OPENCODE_API_URL", "http://127.0.0.1:9999/v1/chat/completions")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.API_URL == "http://127.0.0.1:9999/v1/chat/completions"
    finally:
        monkeypatch.delenv("OPENCODE_API_URL", raising=False)
        importlib.reload(config)


def test_history_file_override_from_environment(monkeypatch, tmp_path):
    """Без этого переключателя любой прогон писал бы в реальный history.json репозитория."""
    target = tmp_path / "custom-history.json"
    monkeypatch.setenv("TABLETOP_HISTORY_FILE", str(target))
    reloaded = importlib.reload(config)
    try:
        assert reloaded.HISTORY_FILE == target
    finally:
        monkeypatch.delenv("TABLETOP_HISTORY_FILE", raising=False)
        importlib.reload(config)


def test_history_file_defaults_into_repository_root(monkeypatch):
    monkeypatch.delenv("TABLETOP_HISTORY_FILE", raising=False)
    reloaded = importlib.reload(config)
    try:
        assert reloaded.HISTORY_FILE == reloaded.BASE_DIR / "history.json"
    finally:
        importlib.reload(config)


def test_base_dir_points_at_repository_root():
    """BASE_DIR поднимается на два уровня от core/config.py — там лежат assets/ и .env."""
    assert (config.BASE_DIR / "assets").is_dir()
    assert (config.BASE_DIR / "tabletop-ai-assistant.py").is_file()


def test_model_name_is_the_reasoning_model():
    assert config.MODEL_NAME == "deepseek-v4-flash"


@pytest.mark.parametrize("attr", ["REQUEST_TIMEOUT", "MAX_RETRIES", "HISTORY_LIMIT", "MAX_INPUT_LENGTH"])
def test_limits_are_positive(attr):
    assert getattr(config, attr) > 0
