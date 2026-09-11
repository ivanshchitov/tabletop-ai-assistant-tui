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


def test_max_words_ceiling_raised_for_reasoning_models():
    """Reasoning-модели (kimi-k2.x, glm-5.1, deepseek-v4-pro) на сложных вопросах тратят весь
    max_tokens на рассуждение раньше, чем дойдут до ответа — 500-словный потолок (2050 токенов)
    этого не покрывает, 1000-словный (4050 токенов) — покрывает (проверено вручную против API).
    """
    assert config.MAX_MAX_WORDS == 1000


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


def test_request_timeout_default_covers_slow_reasoning_models(monkeypatch):
    """kimi-k2.6 на сложном вопросе генерирует дольше 30с — прежний дефолт обрывал запрос раньше,
    чем модель успевала ответить (проверено вручную против реального API)."""
    monkeypatch.delenv("TABLETOP_REQUEST_TIMEOUT", raising=False)
    reloaded = importlib.reload(config)
    try:
        assert reloaded.REQUEST_TIMEOUT == 90
    finally:
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
    assert config.DEFAULT_MODEL == "deepseek-v4-flash"


def test_available_models_is_the_fixed_list():
    assert config.AVAILABLE_MODELS == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "kimi-k2.5",
        "glm-5.1",
        "mimo-v2.5-free",
        "kimi-k2.6",
        "kimi-k3",
    ]


def test_default_model_is_first_available():
    assert config.DEFAULT_MODEL == config.AVAILABLE_MODELS[0]


def test_model_pricing_covers_every_available_model():
    for model in config.AVAILABLE_MODELS:
        assert model in config.MODEL_PRICING
        input_price, output_price = config.MODEL_PRICING[model]
        assert input_price >= 0
        assert output_price >= 0


@pytest.mark.parametrize(
    "attr",
    [
        "REQUEST_TIMEOUT",
        "MAX_RETRIES",
        "MAX_INPUT_LENGTH",
        "SUMMARY_MAX_WORDS",
        "MIN_COMPRESS_AFTER",
        "MAX_COMPRESS_AFTER",
        "MIN_MAX_SESSION_TOKENS",
        "MAX_MAX_SESSION_TOKENS",
    ],
)
def test_limits_are_positive(attr):
    assert getattr(config, attr) > 0


def test_compress_after_defaults_to_10(monkeypatch):
    monkeypatch.delenv("TABLETOP_COMPRESS_AFTER", raising=False)
    reloaded = importlib.reload(config)
    try:
        assert reloaded.DEFAULT_COMPRESS_AFTER == 10
    finally:
        importlib.reload(config)


def test_compress_after_override_from_environment(monkeypatch):
    """Переключатель для e2e: сжатие видно за секунды, а не за 5+ обменов."""
    monkeypatch.setenv("TABLETOP_COMPRESS_AFTER", "5")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.DEFAULT_COMPRESS_AFTER == 5
    finally:
        monkeypatch.delenv("TABLETOP_COMPRESS_AFTER", raising=False)
        importlib.reload(config)


def test_max_session_tokens_defaults_to_20000(monkeypatch):
    monkeypatch.delenv("TABLETOP_MAX_SESSION_TOKENS", raising=False)
    reloaded = importlib.reload(config)
    try:
        assert reloaded.DEFAULT_MAX_SESSION_TOKENS == 20000
    finally:
        importlib.reload(config)


def test_max_session_tokens_override_from_environment(monkeypatch):
    """Переключатель для e2e: потолок контекста сессии проверяется на малом значении."""
    monkeypatch.setenv("TABLETOP_MAX_SESSION_TOKENS", "6000")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.DEFAULT_MAX_SESSION_TOKENS == 6000
    finally:
        monkeypatch.delenv("TABLETOP_MAX_SESSION_TOKENS", raising=False)
        importlib.reload(config)


def test_ranges_are_sane():
    assert config.MIN_MAX_WORDS < config.DEFAULT_MAX_WORDS < config.MAX_MAX_WORDS
    assert config.MIN_LIST_LIMIT <= config.DEFAULT_LIST_LIMIT <= config.MAX_LIST_LIMIT
    assert config.MIN_COMPRESS_AFTER < config.DEFAULT_COMPRESS_AFTER < config.MAX_COMPRESS_AFTER
    assert (
        config.MIN_MAX_SESSION_TOKENS
        < config.DEFAULT_MAX_SESSION_TOKENS
        < config.MAX_MAX_SESSION_TOKENS
    )


def test_summary_max_words_is_a_small_fixed_ceiling():
    """Резюме — компактный дайджест: потолок выхода ограничивает его рост."""
    assert 50 <= config.SUMMARY_MAX_WORDS <= 300
    assert config.max_tokens_for_words(config.SUMMARY_MAX_WORDS) > config.SUMMARY_MAX_WORDS


def test_estimated_chars_per_token_is_positive():
    assert config.ESTIMATED_CHARS_PER_TOKEN > 0


def test_context_strategies_list_is_fixed_with_summary_first():
    assert list(config.CONTEXT_STRATEGIES) == [
        "summary",
        "sliding_window",
        "sticky_facts",
        "branching",
    ]
    assert config.DEFAULT_CONTEXT_STRATEGY == config.CONTEXT_STRATEGIES[0]


def test_facts_limits_bound_the_extractor_output():
    """Блок фактов — компактный словарь: потолок выхода и предел числа ключей."""
    assert 50 <= config.FACTS_MAX_WORDS <= 300
    assert config.max_tokens_for_words(config.FACTS_MAX_WORDS) > config.FACTS_MAX_WORDS
    assert 1 < config.MAX_FACTS_KEYS <= 100
