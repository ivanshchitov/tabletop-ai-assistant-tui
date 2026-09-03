"""Клиент Deepseek API: разбор ответов, ошибки, ретраи, состав запроса."""

import json

import pytest
import requests
import responses

from core import config
from core.api_client import (
    API_KEY_CHARSET_ERROR,
    DeepseekAPIClient,
    DeepseekAPIError,
    is_valid_api_key,
    is_valid_json_answer,
)


@pytest.fixture
def client() -> DeepseekAPIClient:
    return DeepseekAPIClient("sk-test")


def _completion(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


# --- успешный путь --------------------------------------------------------------------


@responses.activate
def test_returns_stripped_content(client):
    responses.add(responses.POST, config.API_URL, json=_completion("  Ответ модели  "), status=200)
    assert client.ask("system", "user") == "Ответ модели"


@responses.activate
def test_request_payload_carries_messages_and_settings(client):
    responses.add(responses.POST, config.API_URL, json=_completion("ok"), status=200)
    client.ask("СИСТЕМА", "ВОПРОС", max_tokens=777)

    payload = json.loads(responses.calls[0].request.body)
    assert payload["model"] == config.MODEL_NAME
    assert payload["temperature"] == config.TEMPERATURE
    assert payload["max_tokens"] == 777
    assert payload["messages"] == [
        {"role": "system", "content": "СИСТЕМА"},
        {"role": "user", "content": "ВОПРОС"},
    ]


@responses.activate
def test_temperature_parameter_reaches_payload(client):
    responses.add(responses.POST, config.API_URL, json=_completion("ok"), status=200)
    client.ask("system", "user", temperature=1.2)
    payload = json.loads(responses.calls[0].request.body)
    assert payload["temperature"] == 1.2


@responses.activate
def test_temperature_defaults_to_config_value(client):
    responses.add(responses.POST, config.API_URL, json=_completion("ok"), status=200)
    client.ask("system", "user")
    payload = json.loads(responses.calls[0].request.body)
    assert payload["temperature"] == config.TEMPERATURE


@responses.activate
def test_payload_never_contains_stop_field(client):
    """Регресс: `stop` обрывает reasoning-модель на reasoning_content с пустым content."""
    responses.add(responses.POST, config.API_URL, json=_completion("ok"), status=200)
    client.ask("system", "user")
    assert "stop" not in json.loads(responses.calls[0].request.body)


@responses.activate
def test_authorization_header_carries_key(client):
    responses.add(responses.POST, config.API_URL, json=_completion("ok"), status=200)
    client.ask("system", "user")
    assert responses.calls[0].request.headers["Authorization"] == "Bearer sk-test"


@responses.activate
def test_default_max_tokens_matches_default_word_limit(client):
    responses.add(responses.POST, config.API_URL, json=_completion("ok"), status=200)
    client.ask("system", "user")
    payload = json.loads(responses.calls[0].request.body)
    assert payload["max_tokens"] == config.max_tokens_for_words(config.DEFAULT_MAX_WORDS)


# --- ошибки ---------------------------------------------------------------------------


@responses.activate
def test_unauthorized_reports_bad_key(client):
    responses.add(responses.POST, config.API_URL, json={"error": "nope"}, status=401)
    with pytest.raises(DeepseekAPIError, match="Неверный API-ключ"):
        client.ask("system", "user")


@responses.activate
def test_server_error_is_wrapped(client):
    responses.add(responses.POST, config.API_URL, json={"error": "boom"}, status=500)
    with pytest.raises(DeepseekAPIError, match="Ошибка Deepseek API"):
        client.ask("system", "user")


@responses.activate
@pytest.mark.parametrize(
    "payload",
    [{}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}],
)
def test_malformed_success_payload_is_wrapped(client, payload):
    responses.add(responses.POST, config.API_URL, json=payload, status=200)
    with pytest.raises(DeepseekAPIError, match="Некорректный ответ"):
        client.ask("system", "user")


@responses.activate
def test_non_json_body_is_wrapped(client):
    responses.add(responses.POST, config.API_URL, body="<html>502</html>", status=200)
    with pytest.raises(DeepseekAPIError, match="Некорректный ответ"):
        client.ask("system", "user")


@responses.activate
def test_connection_error_is_not_retried(client, no_sleep):
    """Проблема с сетью считается устойчивой — повтор бессмысленен, в отличие от таймаута."""
    responses.add(responses.POST, config.API_URL, body=requests.exceptions.ConnectionError())
    with pytest.raises(DeepseekAPIError, match="Ошибка соединения"):
        client.ask("system", "user")
    assert len(responses.calls) == 1
    assert no_sleep == []


# --- ретраи по таймауту ----------------------------------------------------------------


@responses.activate
def test_timeout_is_retried_until_success(client, no_sleep):
    responses.add(responses.POST, config.API_URL, body=requests.exceptions.Timeout())
    responses.add(responses.POST, config.API_URL, body=requests.exceptions.Timeout())
    responses.add(responses.POST, config.API_URL, json=_completion("Наконец-то"), status=200)

    assert client.ask("system", "user") == "Наконец-то"
    assert len(responses.calls) == 3


@responses.activate
def test_timeout_gives_up_after_max_retries(client, no_sleep):
    for _ in range(config.MAX_RETRIES):
        responses.add(responses.POST, config.API_URL, body=requests.exceptions.Timeout())

    with pytest.raises(DeepseekAPIError, match="Превышено время ожидания"):
        client.ask("system", "user")
    assert len(responses.calls) == config.MAX_RETRIES


@responses.activate
def test_backoff_is_exponential(client, no_sleep):
    for _ in range(config.MAX_RETRIES):
        responses.add(responses.POST, config.API_URL, body=requests.exceptions.Timeout())

    with pytest.raises(DeepseekAPIError):
        client.ask("system", "user")
    # Пауза перед каждой повторной попыткой, но не после последней.
    assert no_sleep == [2**i for i in range(config.MAX_RETRIES - 1)]


@responses.activate
def test_http_error_is_not_retried(client, no_sleep):
    """Ретраятся только таймауты: 500 отдаётся пользователю сразу."""
    responses.add(responses.POST, config.API_URL, json={}, status=500)
    with pytest.raises(DeepseekAPIError):
        client.ask("system", "user")
    assert len(responses.calls) == 1


@responses.activate
def test_request_timeout_value_is_passed(client):
    captured = {}

    def callback(request):
        captured["timeout"] = request.req_kwargs.get("timeout")
        return (200, {}, json.dumps(_completion("ok")))

    responses.add_callback(responses.POST, config.API_URL, callback=callback)
    client.ask("system", "user")
    assert captured["timeout"] == config.REQUEST_TIMEOUT


# --- пригодность ключа ------------------------------------------------------------------


@pytest.mark.parametrize("key", ["sk-abc123", "sk-A1_b2-c3.d4", "x" * 200])
def test_ascii_keys_are_accepted(key):
    assert is_valid_api_key(key) is True


@pytest.mark.parametrize("key", ["", "sk-ключ", "sk-clé", "sk-키", "sk-abc\u00a0def"])
def test_non_ascii_or_empty_keys_are_rejected(key):
    assert is_valid_api_key(key) is False


@responses.activate
def test_non_ascii_key_fails_with_a_readable_message():
    """Раньше здесь вылетал UnicodeEncodeError из недр requests — падение с traceback."""
    responses.add(responses.POST, config.API_URL, json=_completion("ok"), status=200)
    with pytest.raises(DeepseekAPIError, match="проверьте раскладку клавиатуры"):
        DeepseekAPIClient("sk-ключ-в-кириллице").ask("system", "user")


@responses.activate
def test_bad_key_is_rejected_before_any_request():
    """Заведомо непригодный ключ не должен приводить к обращению к API."""
    responses.add(responses.POST, config.API_URL, json=_completion("ok"), status=200)
    with pytest.raises(DeepseekAPIError):
        DeepseekAPIClient("sk-кириллица").ask("system", "user")
    assert len(responses.calls) == 0


@responses.activate
def test_empty_key_is_reported_too():
    responses.add(responses.POST, config.API_URL, json=_completion("ok"), status=200)
    with pytest.raises(DeepseekAPIError, match=API_KEY_CHARSET_ERROR[:20]):
        DeepseekAPIClient("").ask("system", "user")


# --- проверка JSON-ответа ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '{"name_ru": "Каркассон"}',
        '  {"a": 1}  ',
        '```json\n{"a": 1}\n```',
        '```json\n{\n  "name_ru": "Catan",\n  "genre": "евро"\n}\n```',
        "[1, 2, 3]",
    ],
)
def test_valid_json_answers(text):
    assert is_valid_json_answer(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Каркассон — отличная игра",
        '{"name_ru": "Каркассон"',
        "```json\nне json\n```",
        "Вот ответ: {броken}",
    ],
)
def test_invalid_json_answers(text):
    assert is_valid_json_answer(text) is False


def test_json_block_wins_over_surrounding_prose():
    """Модель иногда добавляет текст вокруг блока — блок всё равно должен быть распознан."""
    assert is_valid_json_answer('Вот карточка:\n```json\n{"a": 1}\n```\nГотово!') is True
