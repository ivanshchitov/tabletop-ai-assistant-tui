"""Агент: стек сообщений сессии, пересылка LLM, решение логической задачи."""

import json
from typing import List, Optional

import pytest
from core import config, logictask, prompts, tabletop_agent
from core.usage import estimate_tokens
from core.answer_settings import AnswerFormat, AnswerSettings
from core.api_client import AnswerMeta, APIError
from core.history_manager import HistoryManager
from core.tabletop_agent import TabletopAgent


class FakeAgentClient:
    """Подставной клиент в терминах сообщений: запоминает полный список messages.

    temperature=None означает «вызывающий не передал температуру» — так отличают вызов
    /logictask (клиентский дефолт) от явной передачи значения настройки.
    """

    def __init__(self, answers=None, error: Optional[Exception] = None, usages=None) -> None:
        self.answers = list(answers or ["Ответ по умолчанию"])
        self.error = error
        # Список (prompt_tokens, completion_tokens, cost_usd) — по одному на вызов;
        # последний повторяется, если вызовов больше.
        self.usages = list(usages) if usages else None
        self.calls: List[dict] = []

    def ask_with_usage_messages(
        self,
        messages,
        max_tokens: int = 0,
        temperature: Optional[float] = None,
        model: Optional[str] = None,
    ) -> AnswerMeta:
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "max_tokens": max_tokens,
                "temperature": temperature,
                "model": model,
            }
        )
        if self.error is not None:
            raise self.error
        content = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if self.usages:
            prompt, completion, cost = self.usages.pop(0) if len(self.usages) > 1 else self.usages[0]
        else:
            prompt, completion, cost = 10, 20, 0.0001
        return AnswerMeta(
            content=content,
            model=model or config.DEFAULT_MODEL,
            elapsed_seconds=0.01,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cost_usd=cost,
        )
@pytest.fixture(autouse=True)
def isolated_history(tmp_path, monkeypatch):
    """Дефолтный HistoryManager агента всегда указывает на временный файл.

    Агент владеет памятью и восстанавливает её сам при создании: без патча каждый
    тест читал бы реальный history.json пользователя.
    """
    monkeypatch.setattr(
        tabletop_agent, "HistoryManager", lambda: HistoryManager(tmp_path / "history.json")
    )


def make_agent(client=None, answers=None, history=None, usages=None, **kwargs) -> tuple:
    client = client if client is not None else FakeAgentClient(answers=answers, usages=usages)
    if history is not None:
        return TabletopAgent(client, history=history, **kwargs), client
    return TabletopAgent(client, **kwargs), client


def user_contents(messages) -> List[str]:
    return [m["content"] for m in messages if m["role"] == "user"]


# --- стек сообщений -------------------------------------------------------------------


def test_first_ask_sends_system_and_user_only():
    agent, client = make_agent()
    agent.ask("Первый вопрос")

    messages = client.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "Tabletop AI Assistant" in messages[0]["content"]
    assert "Первый вопрос" in messages[1]["content"]


def test_second_ask_carries_previous_exchange():
    agent, client = make_agent()
    agent.ask("Первый вопрос")
    agent.ask("Второй вопрос")

    messages = client.calls[1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert "Первый вопрос" in messages[1]["content"]
    assert "Ответ по умолчанию" in messages[2]["content"]
    assert "Второй вопрос" in messages[3]["content"]


def test_failed_exchange_is_not_remembered():
    agent, client = make_agent()
    agent.ask("Успешный вопрос")
    client.error = APIError("Ошибка соединения с API.")

    with pytest.raises(APIError):
        agent.ask("Провальный вопрос")
    client.error = None
    agent.ask("Третий вопрос")
    assert "Провальный вопрос" not in "".join(user_contents(client.calls[2]["messages"]))
    assert [m["role"] for m in client.calls[2]["messages"]][1:] == ["user", "assistant", "user"]


def test_stack_is_capped_at_history_limit_exchanges(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_LIMIT", 2)
    agent, client = make_agent()
    for number in range(1, 5):
        agent.ask(f"Вопрос {number}")

    messages = client.calls[-1]["messages"]
    # system + последние 2 завершённых обмена (4 сообщения) + текущий user-ход = 6
    assert len(messages) == 6
    contents = "".join(m["content"] for m in messages)
    assert "Вопрос 4" in contents
    assert "Вопрос 3" in contents
    assert "Вопрос 1" not in contents


def test_reset_clears_the_stack():
    agent, client = make_agent()
    agent.ask("Вопрос до очистки")
    agent.reset()
    agent.ask("Вопрос после очистки")

    messages = client.calls[-1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "Вопрос до очистки" not in "".join(m["content"] for m in messages)


# --- долговременная память внутри агента ---------------------------------------------------


def test_agent_restores_its_own_history_on_construction():
    first, _ = make_agent()
    first.history.add("Вопрос из истории", "Ответ из истории")

    second, client = make_agent(history=first.history, answers=["Свежий ответ"])
    second.ask("Новый вопрос")

    messages = client.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert "Вопрос из истории" in messages[1]["content"]
    assert messages[2]["content"] == "Ответ из истории"
    assert "Новый вопрос" in messages[-1]["content"]


def test_restored_user_turns_carry_current_settings_instructions():
    first, _ = make_agent()
    first.history.add("Старый вопрос", "Старый ответ")

    second, client = make_agent(
        history=first.history, settings=AnswerSettings(max_words=500, list_limit=7)
    )
    second.ask("Новый вопрос")

    restored_user = client.calls[0]["messages"][1]["content"]
    assert "Старый вопрос" in restored_user
    assert "не более 500 слов" in restored_user
    assert "не более 7 вариантов" in restored_user


def test_memory_is_capped_at_history_limit(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_LIMIT", 2)
    first, _ = make_agent()
    for i in range(1, 5):
        first.history.add(f"Вопрос {i}", f"Ответ {i}")

    second, client = make_agent(history=first.history)
    second.ask("Новый вопрос")

    contents = "".join(m["content"] for m in client.calls[0]["messages"])
    assert "Вопрос 1" not in contents and "Вопрос 2" not in contents
    assert "Вопрос 3" in contents and "Вопрос 4" in contents

def test_ask_persists_exchange_immediately():
    agent, _ = make_agent()
    agent.ask("Вопрос на диск")
    data = json.loads(agent.history.path.read_text(encoding="utf-8"))
    assert data[0]["question"] == "Вопрос на диск"
    assert data[0]["answer"] == "Ответ по умолчанию"
    assert data[0]["usage"]["total_tokens"] == 30


def test_failed_exchange_is_not_persisted():
    agent, _ = make_agent(client=FakeAgentClient(error=APIError("Сбой API.")))
    with pytest.raises(APIError):
        agent.ask("Вопрос при ошибке")

    assert agent.history.dialogues == []


def test_reset_clears_stack_and_file():
    agent, client = make_agent()
    agent.ask("Вопрос до очистки")
    agent.reset()

    assert json.loads(agent.history.path.read_text(encoding="utf-8")) == []
    assert agent.history.dialogues == []

    agent.ask("Вопрос после очистки")
    messages = client.calls[-1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "Вопрос до очистки" not in messages[1]["content"]


def test_logictask_does_not_touch_memory():
    agent, _ = make_agent(answers=["Прямой ответ", "Ещё ответ"])
    agent.ask("Обычный вопрос")

    list(agent.solve_logictask(1))

    data = json.loads(agent.history.path.read_text(encoding="utf-8"))
    assert [d["question"] for d in data] == ["Обычный вопрос"]


def test_stack_is_rebuilt_from_file_invariant():
    first, first_client = make_agent(answers=["Первый ответ", "Второй ответ"])
    first.ask("Первый вопрос")
    first.ask("Второй вопрос")
    reference = first_client.calls[-1]["messages"]

    second, second_client = make_agent(history=first.history, answers=["Третий ответ"])
    second.ask("Третий вопрос")

    restored = second_client.calls[0]["messages"]
    # system + два восстановленных обмена (user/assistant) + новый user-ход
    assert [m["role"] for m in restored] == ["system", "user", "assistant", "user", "assistant", "user"]
    # восстановленный стек дословно совпадает со стеком первого агента на момент записи
    assert [m["content"] for m in restored[1:4]] == [m["content"] for m in reference[1:4]]
    assert restored[4]["content"] == "Второй ответ"
    assert "Третий вопрос" in restored[5]["content"]


# --- системное сообщение и параметры ---------------------------------------------------


def test_system_message_is_rebuilt_from_current_settings():
    agent, client = make_agent()
    agent.ask("Вопрос до смены формата")
    agent.settings = agent.settings.with_format(AnswerFormat.JSON)
    agent.ask("Вопрос после смены формата")

    assert "Формат ответа: JSON" in client.calls[1]["messages"][0]["content"]
    assert "Формат ответа" not in client.calls[0]["messages"][0]["content"]


def test_past_user_turns_keep_their_own_word_limit():
    agent, client = make_agent()
    agent.ask("Вопрос с лимитом 200")
    agent.settings = agent.settings.with_max_words(500)
    agent.ask("Вопрос с лимитом 500")

    messages = client.calls[1]["messages"]
    assert "не более 200 слов" in messages[1]["content"]
    assert "не более 500 слов" in messages[3]["content"]


def test_ask_passes_session_settings_and_model():
    agent, client = make_agent(
        settings=AnswerSettings(max_words=300, temperature=1.1), model="kimi-k2.6"
    )
    agent.ask("Вопрос")

    call = client.calls[0]
    assert call["max_tokens"] == config.max_tokens_for_words(300)
    assert call["temperature"] == 1.1
    assert call["model"] == "kimi-k2.6"


def test_ask_returns_answer_meta():
    agent, _ = make_agent()
    meta = agent.ask("Вопрос")
    assert meta.content == "Ответ по умолчанию"
    assert meta.total_tokens == 30


# --- логическая задача -----------------------------------------------------------------


def test_logictask_strategy_1_sends_one_call_outside_the_stack():
    agent, client = make_agent()
    agent.ask("Обычный вопрос")
    results = list(agent.solve_logictask(1))

    assert len(results) == 1
    label, meta = results[0]
    assert meta.total_tokens == 30
    call = client.calls[1]
    assert [m["role"] for m in call["messages"]] == ["system", "user"]
    assert call["messages"][0]["content"] == logictask.DIRECT_SYSTEM_MESSAGE
    assert logictask.LOGIC_TASK in call["messages"][1]["content"]
    assert call["max_tokens"] == config.max_tokens_for_words(config.DEFAULT_MAX_WORDS)
    assert call["temperature"] is None  # клиентский дефолт, настройки сессии не применяются
    # стек после прогона идентичен стеку до него


def test_logictask_does_not_touch_the_conversation_stack():
    agent, client = make_agent()
    agent.ask("Обычный вопрос")
    stack_before = [dict(m) for m in client.calls[0]["messages"]]

    list(agent.solve_logictask(1))

    agent.ask("Следующий вопрос")
    messages = client.calls[-1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert all(
        before["role"] == after["role"] and before["content"] == after["content"]
        for before, after in zip(stack_before, messages)
    )


def test_logictask_strategy_3_makes_two_calls_with_labels():
    agent, client = make_agent(answers=["СОСТАВЛЕННЫЙ ПРОМПТ", "РЕШЕНИЕ"])
    results = list(agent.solve_logictask(3))

    assert [label for label, _ in results] == [
        "Составленный моделью промпт",
        None,
    ]
    assert client.calls[0]["messages"][0]["content"] == logictask.COMPOSE_SYSTEM_MESSAGE
    assert client.calls[1]["messages"][0]["content"] == "СОСТАВЛЕННЫЙ ПРОМПТ"
    assert client.calls[1]["messages"][1]["content"] == logictask.LOGIC_TASK


def test_logictask_strategy_4_makes_three_expert_calls():
    agent, client = make_agent()
    results = list(agent.solve_logictask(4))

    assert len(results) == 3
    assert [label for label, _ in results] == list(logictask.EXPERT_ROLES)
    assert [c["messages"][0]["content"] for c in client.calls] == list(logictask.EXPERT_ROLES)


def test_logictask_strategy_2_sends_one_call():
    agent, client = make_agent()
    results = list(agent.solve_logictask(2))
    assert len(results) == 1
    assert results[0][0] is None
    assert logictask.LOGIC_TASK in client.calls[0]["messages"][1]["content"]


# --- конфиг агента и метрики последнего запроса -----------------------------------------


def test_config_exposes_all_five_settings():
    agent, _ = make_agent(
        settings=AnswerSettings(max_words=300, temperature=1.1), model="kimi-k2.6"
    )

    assert agent.config.format is AnswerFormat.FREE
    assert agent.config.max_words == 300
    assert agent.config.list_limit == config.DEFAULT_LIST_LIMIT
    assert agent.config.temperature == 1.1
    assert agent.config.model == "kimi-k2.6"


def test_config_model_change_reaches_next_request():
    agent, client = make_agent()
    agent.config.model = "kimi-k3"
    agent.ask("Вопрос")

    assert client.calls[0]["model"] == "kimi-k3"


def test_last_result_is_none_before_any_request():
    agent, _ = make_agent()
    assert agent.last_result is None


def test_last_result_holds_metrics_of_the_last_question():
    agent, client = make_agent(answers=["Первый", "Второй"])
    agent.ask("Первый вопрос")
    agent.ask("Второй вопрос")

    assert agent.last_result.content == "Второй"
    assert agent.last_result.elapsed_seconds >= 0.0
    assert agent.last_result.total_tokens == 30
    assert agent.last_result.cost_usd == 0.0001


def test_last_result_is_updated_by_logictask_calls():
    agent, client = make_agent(answers=["Первый", "РЕШЕНИЕ ЗАДАЧИ"])
    agent.ask("Обычный вопрос")
    list(agent.solve_logictask(2))

    assert agent.last_result.content == "РЕШЕНИЕ ЗАДАЧИ"


def test_last_result_is_read_only():
    agent, _ = make_agent()
    with pytest.raises(AttributeError):
        agent.last_result = None


# --- день 8: накопитель сессии, расход истории, оценка стека ---------------------------


def test_session_usage_accumulates_successful_questions():
    agent, _ = make_agent()
    agent.ask("Первый")
    agent.ask("Второй")
    usage = agent.session_usage
    assert usage.requests == 2
    assert usage.total_tokens == 60
    assert usage.cost_usd == 0.0002


def test_session_usage_counts_logictask_calls():
    agent, _ = make_agent(answers=["Прямой ответ"])
    list(agent.solve_logictask(1))
    assert agent.session_usage.requests == 1


def test_failed_request_is_not_counted():
    agent, _ = make_agent(client=FakeAgentClient(error=APIError("Сбой API.")))
    with pytest.raises(APIError):
        agent.ask("Вопрос")
    assert agent.session_usage.requests == 0


def test_reset_clears_session_usage():
    agent, _ = make_agent()
    agent.ask("Вопрос")
    agent.reset()
    assert agent.session_usage.requests == 0


def test_ask_persists_usage_block_in_history():
    agent, _ = make_agent(usages=[(100, 50, 0.01)])
    agent.ask("Вопрос на диск")
    record = agent.history.dialogues[0]
    assert record["usage"] == {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cost_usd": 0.01,
    }


def test_lifetime_usage_survives_restart_through_history_file():
    first, _ = make_agent(usages=[(100, 50, 0.01)])
    first.ask("Вопрос между запусками")
    second, _ = make_agent()
    assert second.session_usage.requests == 0  # сессия новая
    lifetime = second.history.total_usage()
    assert lifetime.requests == 1
    assert lifetime.total_tokens == 150

def test_stack_tokens_estimate_counts_system_message():
    agent, _ = make_agent()
    assert agent.stack_tokens_estimate == estimate_tokens(
        prompts.build_system_message(agent.config.format)
    )


def test_stack_tokens_estimate_grows_with_turns():
    agent, _ = make_agent()
    before = agent.stack_tokens_estimate
    agent.ask("Вопрос")
    assert agent.stack_tokens_estimate > before


def test_stack_tokens_estimate_stops_growing_when_window_is_full(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_LIMIT", 2)
    agent, _ = make_agent()
    agent.ask("Вопрос 1")
    agent.ask("Вопрос 2")
    full = agent.stack_tokens_estimate
    agent.ask("Вопрос 3")
    assert agent.stack_tokens_estimate == full
