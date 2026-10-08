"""Параметры реально отправленных запросов, возврат профиля и ограничения агента."""

import json

import pytest
import responses

from core import config
from core.answer_settings import AnswerFormat, AnswerSettings
from core.api_client import APIClient
from core.history_manager import HistoryManager
from core.long_term_memory import LongTermMemory
from core.schedule_store import ScheduleStore
from core.tabletop_agent import TabletopAgent, RequestPhase
from core.task_state import TaskStore
from core.user_profile import ProfileStore

BASE_M = "unsloth/Qwen3.5-2B-GGUF:Q4_K_M"
BASE_XL = "unsloth/Qwen3.5-2B-GGUF:Q4_K_XL"
OPT_M = "tabletop-qwen-2b-q4-k-m-optimized"
OPT_XL = "tabletop-qwen-2b-q4-k-xl-optimized"


@pytest.fixture
def agent(tmp_path):
    result = TabletopAgent(
        APIClient("sk-test"), model=BASE_M,
        history=HistoryManager(tmp_path / "history.json"),
        long_term=LongTermMemory(tmp_path / "memory.json"),
        profile=ProfileStore(tmp_path / "profile.json"),
        task=TaskStore(tmp_path / "task.json"),
        schedule=ScheduleStore(tmp_path / "schedule.json"),
        task_results_dir=tmp_path / "tasks",
    )
    result.rag_enabled = False
    result.config.auto_tools = False
    return result


def reply(content="Конь ходит буквой Г."):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 30, "completion_tokens": 10}}


@responses.activate
@pytest.mark.parametrize("baseline,optimized", [(BASE_M, OPT_M), (BASE_XL, OPT_XL)])
def test_optimized_question_applies_parameters_and_returns_to_original(agent, baseline, optimized):
    agent.model = baseline
    agent.settings = AnswerSettings(temperature=1.2, max_words=70)
    responses.add(responses.POST, config.LOCAL_API_URL, json=reply())
    agent.switch_local_profile("optimized")
    result = agent.ask("Как ходит конь в шахматах?")
    sent = json.loads(responses.calls[-1].request.body)
    assert sent["model"] == optimized
    assert (sent["temperature"], sent["max_tokens"]) == (0.3, 1024)
    assert "Authorization" not in responses.calls[-1].request.headers
    assert result.cost_usd == 0.0
    assert "не более 70 слов" in sent["messages"][-1]["content"]
    assert "Покажи расчёт" in sent["messages"][0]["content"]
    assert agent.settings.temperature == 1.2
    agent.switch_local_profile("baseline")
    agent.ask("Как ходит конь в шахматах?")
    sent = json.loads(responses.calls[-1].request.body)
    assert sent["model"] == baseline
    assert (sent["temperature"], sent["max_tokens"]) == (1.2, 2000)
    assert "Покажи расчёт" not in sent["messages"][0]["content"]


@responses.activate
@pytest.mark.parametrize("fmt", [AnswerFormat.JSON, AnswerFormat.COMPACT])
def test_optimized_template_keeps_format_and_invariants(agent, fmt):
    from core.prompts import get_format_instruction
    from core.invariants import invariants_message
    agent.model = OPT_M
    agent.settings = agent.settings.with_format(fmt)
    responses.add(responses.POST, config.LOCAL_API_URL, json=reply("{}"))
    agent.ask("Опиши шахматы")
    system = json.loads(responses.calls[0].request.body)["messages"][0]["content"]
    assert get_format_instruction(fmt) in system
    assert invariants_message() in system


@responses.activate
def test_optimized_invariant_retry_keeps_budget(agent):
    agent.model = OPT_M
    responses.add(responses.POST, config.LOCAL_API_URL, json=reply("Посетите казино."))
    responses.add(responses.POST, config.LOCAL_API_URL, json=reply())
    agent.ask("Как ходит конь?")
    assert len(responses.calls) == 2
    for call in responses.calls:
        sent = json.loads(call.request.body)
        assert (sent["temperature"], sent["max_tokens"]) == (0.3, 1024)


@responses.activate
def test_auxiliary_request_is_not_capped_by_answer_profile(agent):
    agent.model = OPT_M
    responses.add(responses.POST, config.LOCAL_API_URL, json=reply("{}"))
    agent._ask_rules([{"role": "user", "content": "поиск"}], 50, RequestPhase.RULES_REWRITE, None)
    sent = json.loads(responses.calls[0].request.body)
    assert (sent["temperature"], sent["max_tokens"]) == (0.7, 2000)


@responses.activate
def test_cloud_question_keeps_session_settings_and_template(agent):
    agent.model = config.DEFAULT_MODEL
    agent.settings = AnswerSettings(temperature=1.2, max_words=70)
    responses.add(responses.POST, config.API_URL, json=reply())
    agent.ask("Как ходит конь?")
    sent = json.loads(responses.calls[0].request.body)
    assert (sent["temperature"], sent["max_tokens"]) == (1.2, 2000)
    assert "Покажи расчёт" not in sent["messages"][0]["content"]
    assert responses.calls[0].request.headers["Authorization"] == "Bearer sk-test"


@pytest.mark.parametrize("model,mode", [(config.DEFAULT_MODEL, "optimized"),
    ("unsloth/Qwen3.5-4B-GGUF:Q4_K_M", "baseline"), (BASE_M, "wrong")])
def test_invalid_switch_preserves_model_and_settings(agent, model, mode):
    agent.model = model
    before = agent.settings
    with pytest.raises(ValueError):
        agent.switch_local_profile(mode)
    assert agent.model == model
    assert agent.settings is before


def test_report_describes_actual_profile_without_network(agent):
    agent.model = OPT_XL
    report = agent.local_report()
    assert (report.profile, report.quantization, report.temperature, report.max_tokens,
            report.context_window) == ("optimized", "Q4_K_XL", 0.3, 1024, 8192)


@responses.activate
def test_benchmark_seed_and_cache_options_only_go_to_local_api():
    client = APIClient("sk-test", seed=42, cache_prompt=False)
    responses.add(responses.POST, config.LOCAL_API_URL, json=reply())
    responses.add(responses.POST, config.API_URL, json=reply())
    client.ask("s", "u", model=BASE_M)
    client.ask("s", "u", model=config.DEFAULT_MODEL)
    local, cloud = [json.loads(call.request.body) for call in responses.calls]
    assert local["seed"] == 42 and local["cache_prompt"] is False
    assert "seed" not in cloud and "cache_prompt" not in cloud
