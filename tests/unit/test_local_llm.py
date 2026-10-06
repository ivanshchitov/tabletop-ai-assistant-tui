"""Локальные пресеты, адреса и отсутствие облачного ключа в локальных запросах."""

import json

import pytest
import responses

from core import config
from core.api_client import APIClient, APIError


QWEN = "unsloth/Qwen3.5-2B-GGUF:Q4_K_M"


def test_presets_supply_models_in_order_without_global_section(tmp_path):
    presets = tmp_path / "models.ini"
    presets.write_text("version = 1\n[*]\nc = 8192\n[qwen-small]\ntemp=0.7\n[qwen-big]\n", encoding="utf-8")
    assert config.local_models(presets) == ["qwen-small", "qwen-big"]
    assert config.local_models(tmp_path / "absent.ini") == []


def test_qwen_models_follow_cloud_models_in_panel():
    from ui.models_screen import initial_state
    state = initial_state(QWEN)
    assert state.selected == QWEN
    assert state.available[-2:] == [QWEN, "unsloth/Qwen3.5-4B-GGUF:Q4_K_M"]
    assert config.MODEL_PRICING[QWEN] == (0.0, 0.0)


@responses.activate
@pytest.mark.parametrize("key", ["", "sk-cloud-secret", "ключ"])
def test_local_request_needs_no_key_and_never_sends_cloud_secret(key):
    responses.add(responses.POST, "http://127.0.0.1:9999/v1/chat/completions", json={
        "choices": [{"message": {"content": "Конь ходит буквой Г."}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    })
    result = APIClient(key).ask_with_usage_messages([{"role": "user", "content": "Как ходит конь?"}], model=QWEN)
    request = responses.calls[0].request
    assert "Authorization" not in request.headers
    assert json.loads(request.body)["model"] == QWEN
    assert result.content == "Конь ходит буквой Г."
    assert result.cost_usd == 0.0
    assert result.total_tokens == 30


@responses.activate
def test_switching_back_to_cloud_restores_url_and_authorization():
    payload = {"choices": [{"message": {"content": "ok"}}]}
    responses.add(responses.POST, "http://127.0.0.1:9999/v1/chat/completions", json=payload)
    responses.add(responses.POST, config.API_URL, json=payload)
    client = APIClient("sk-cloud")
    client.ask("system", "user", model=QWEN)
    client.ask("system", "user", model=config.DEFAULT_MODEL)
    assert responses.calls[1].request.headers["Authorization"] == "Bearer sk-cloud"


@responses.activate
def test_local_address_override_is_used(monkeypatch):
    monkeypatch.setenv("TABLETOP_LOCAL_API_URL", "http://127.0.0.1:12345/v1/chat/completions")
    responses.add(responses.POST, "http://127.0.0.1:12345/v1/chat/completions", json={"choices": [{"message": {"content": "ok"}}]})
    assert APIClient("").ask("s", "u", model=QWEN) == "ok"


def test_cloud_request_without_key_reports_how_to_configure_it():
    with pytest.raises(APIError, match="OPENCODE_API_KEY"):
        APIClient("").ask("s", "u")


@responses.activate
def test_local_qwen_receives_one_leading_system_message_with_all_instructions():
    responses.add(responses.POST, config.LOCAL_API_URL, json={"choices": [{"message": {"content": "ok"}}]})
    messages = [
        {"role": "system", "content": "Настройки ответа"},
        {"role": "system", "content": "Инварианты"},
        {"role": "system", "content": "Память"},
        {"role": "user", "content": "Вопрос"},
    ]
    APIClient("").ask_with_usage_messages(messages, model=QWEN)
    sent = json.loads(responses.calls[0].request.body)["messages"]
    assert sent == [
        {"role": "system", "content": "Настройки ответа\n\nИнварианты\n\nПамять"},
        {"role": "user", "content": "Вопрос"},
    ]
    assert len(messages) == 4  # исходный стек агента не меняется
