"""Агент: стек сообщений сессии, пересылка LLM, решение логической задачи."""

import json
from typing import List, Optional

import pytest
from core import config, context_compressor, logictask, prompts, tabletop_agent
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


def test_threshold_reached_collapses_stack_to_summary_and_tail():
    """Порог в сообщениях: при достижении суммаризатор сворачивает всё, кроме последнего обмена."""
    agent, client = make_agent(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6"]
    )
    for i in range(1, 6):
        agent.ask(f"Вопрос {i}")
    meta = agent.ask("Вопрос 6")

    assert meta.content == "Ответ 6"
    summary_call, question_call = client.calls[5], client.calls[6]
    assert summary_call["messages"][0]["content"] == context_compressor.summary_instruction()
    summary_user = summary_call["messages"][1]["content"]
    assert "Вопрос 1" in summary_user and "Ответ 4" in summary_user
    assert "Вопрос 5" not in summary_user and "Ответ 5" not in summary_user
    assert [m["role"] for m in question_call["messages"]] == [
        "system", "system", "user", "assistant", "user",
    ]
    assert "РЕЗЮМЕ 1" in question_call["messages"][1]["content"]
    assert "Вопрос 5" in question_call["messages"][2]["content"]
    assert question_call["messages"][3]["content"] == "Ответ 5"
    assert "Вопрос 6" in question_call["messages"][4]["content"]
    assert agent.stack_exchanges == 2  # хвост (обмен 5) + новый обмен 6


def test_summarizer_error_keeps_stack_and_summary_intact():
    """Ходы удаляются из стека только после успешного суммаризатора."""
    class FlakySummarizer(FakeAgentClient):
        def __init__(self):
            super().__init__(
                answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "Ответ 6"]
            )
            self.summarizer_calls = 0

        def ask_with_usage_messages(self, messages, **kwargs):
            if messages[0]["content"] == context_compressor.summary_instruction():
                self.summarizer_calls += 1
                raise APIError("Сбой суммаризатора.")
            return super().ask_with_usage_messages(messages, **kwargs)

    client = FlakySummarizer()
    agent, _ = make_agent(client=client)
    for i in range(1, 6):
        agent.ask(f"Вопрос {i}")

    with pytest.raises(APIError):
        agent.ask("Вопрос 6")
    assert agent.stack_exchanges == 5
    assert agent._summary is None
    assert [d["question"] for d in agent.history.dialogues] == [f"Вопрос {i}" for i in range(1, 6)]

    with pytest.raises(APIError):
        agent.ask("Вопрос 6 ещё раз")
    assert client.summarizer_calls == 2  # следующий вопрос повторяет попытку сжатия

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


def test_memory_is_unbounded_and_restore_seeds_summary_with_tail():
    """Файл не вытесняется: рестарт восстанавливает резюме и неотжатый хвост целиком."""
    first, _ = make_agent(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6"]
    )
    for i in range(1, 7):
        first.ask(f"Вопрос {i}")  # сжатие на 6-м вопросе: резюме обменов 1-4

    second, client = make_agent(history=first.history, answers=["Ответ 7"])
    second.ask("Вопрос 7")

    messages = client.calls[0]["messages"]
    contents = "".join(m["content"] for m in messages)
    assert "Вопрос 1" not in contents and "Вопрос 4" not in contents  # под резюме
    assert "РЕЗЮМЕ 1" in contents
    assert "Вопрос 5" in contents and "Вопрос 6" in contents  # неотжатый хвост

def test_ask_persists_exchange_immediately():
    agent, _ = make_agent()
    agent.ask("Вопрос на диск")
    data = json.loads(agent.history.path.read_text(encoding="utf-8"))
    assert data["dialogues"][0]["question"] == "Вопрос на диск"
    assert data["dialogues"][0]["answer"] == "Ответ по умолчанию"
    assert data["dialogues"][0]["usage"]["total_tokens"] == 30
    assert data["summary"] is None  # без сжатия резюме пусто


def test_failed_exchange_is_not_persisted():
    agent, _ = make_agent(client=FakeAgentClient(error=APIError("Сбой API.")))
    with pytest.raises(APIError):
        agent.ask("Вопрос при ошибке")

    assert agent.history.dialogues == []


def test_reset_clears_stack_and_file():
    agent, client = make_agent()
    agent.ask("Вопрос до очистки")
    agent.reset()

    assert json.loads(agent.history.path.read_text(encoding="utf-8")) == {
        "summary": None,
        "summary_covers": 0,
        "dialogues": [],
    }
    assert agent.history.dialogues == []

    agent.ask("Вопрос после очистки")
    messages = client.calls[-1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "Вопрос до очистки" not in messages[1]["content"]


def test_logictask_does_not_touch_memory():
    agent, _ = make_agent(answers=["Прямой ответ", "Ещё ответ"])
    agent.ask("Обычный вопрос")

    data = json.loads(agent.history.path.read_text(encoding="utf-8"))
    assert [d["question"] for d in data["dialogues"]] == ["Обычный вопрос"]



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




# --- день 9: сжатие истории ------------------------------------------------------------


def _padded(answer: str) -> str:
    return f"{answer} " + "х" * 200


def test_summary_is_used_in_following_requests_without_resummarizing():
    """Резюме участвует в запросах; покрытые обмены не суммаризуются повторно."""
    agent, client = make_agent(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6", "Ответ 7"]
    )
    for i in range(1, 7):
        agent.ask(f"Вопрос {i}")
    agent.ask("Вопрос 7")

    # вызовы: 5 вопросов, суммаризатор, вопрос 6, вопрос 7 — второго суммаризатора нет
    assert len(client.calls) == 8
    question7 = client.calls[7]
    assert [m["role"] for m in question7["messages"]] == [
        "system", "system", "user", "assistant", "user", "assistant", "user",
    ]
    assert "РЕЗЮМЕ 1" in question7["messages"][1]["content"]
    contents = "".join(m["content"] for m in question7["messages"])
    assert "Вопрос 1" not in contents and "Вопрос 4" not in contents  # под резюме
    assert "Вопрос 5" in contents and "Вопрос 6" in contents


def test_ceiling_estimate_above_limit_triggers_early_compression(monkeypatch):
    """Потолок токенов: оценка выше потолка сжимает досрочно, независимо от порога.

    EPT=1: система ~1650 ток.; 3 обмена с ответами ~700 симв. дают ~3225 ток. ходов —
    при потолке 5000 сжатие срабатывает на четвёртом вопросе при raw=6 < порога.
    """
    monkeypatch.setattr(config, "ESTIMATED_CHARS_PER_TOKEN", 1)
    agent, client = make_agent(
        answers=[f"Ответ {i} " + "х" * 700 for i in range(1, 4)]
        + ["РЕЗЮМЕ 1", "Ответ 4", "Ответ 5"]
    )
    agent.settings = agent.settings.with_max_session_tokens(5000)
    for i in range(1, 4):
        agent.ask(f"Вопрос {i}")  # raw=6, оценка ещё под потолком
    agent.ask("Вопрос 4")  # оценка выше потолка: суммаризатор + вопрос
    agent.ask("Вопрос 5")  # порог не достигнут: только вопрос

    assert len(client.calls) == 6
    summary_call, question_call = client.calls[3], client.calls[4]
    assert summary_call["messages"][0]["content"] == context_compressor.summary_instruction()
    assert [m["role"] for m in question_call["messages"]] == [
        "system", "system", "user", "assistant", "user",
    ]
    assert "РЕЗЮМЕ 1" in question_call["messages"][1]["content"]
    assert "Вопрос 3" in question_call["messages"][2]["content"]


def test_stack_tokens_estimate_collapses_after_digest():
    """Оценка стека после сжатия падает: резюме + последний обмен вместо десяти ходов."""
    agent, client = make_agent(
        answers=[f"Ответ {i} " + "х" * 700 for i in range(1, 6)]
        + ["РЕЗЮМЕ 1", "Ответ 6"]
    )
    for i in range(1, 6):
        agent.ask(f"Вопрос {i}")
    before = agent.stack_tokens_estimate
    agent.ask("Вопрос 6")
    assert agent.stack_tokens_estimate < before


def test_ceiling_unreachable_when_nothing_to_digest(monkeypatch):
    """Сжимать нечего (только последний обмен) — запрос уходит как есть, ходы не теряются."""
    monkeypatch.setattr(config, "ESTIMATED_CHARS_PER_TOKEN", 1)
    agent, client = make_agent(answers=["Ответ 1", "Ответ 2"])
    agent.settings = agent.settings.with_max_session_tokens(5000)
    agent.ask("Вопрос 1")  # raw=2, оценка ~1650+374+360 < 5000 — без сжатия
    assert len(client.calls) == 1

    agent.ask("в" * 12000)  # оценка системы + ходов + промпта > 5000; сворачивать
    # нечего: в стеке только последний обмен — запрос уходит как есть, ходы целы

    assert len(client.calls) == 2  # суммаризатора не было
    assert [m["role"] for m in client.calls[1]["messages"]][:2] == ["system", "user"]


def test_summarizer_spend_is_counted_and_last_result_is_answers():
    agent, client = make_agent(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6"],
        usages=[(10, 20, 0.0001)] * 5 + [(50, 30, 0.001), (100, 60, 0.002)],
    )
    for i in range(1, 6):
        agent.ask(f"Вопрос {i}")
    meta = agent.ask("Вопрос 6")

    assert agent.session_usage.requests == 7
    assert agent.session_usage.cost_usd == pytest.approx(0.0001 * 5 + 0.001 + 0.002)
    # _last_result после ask() — метрика ответа, а не суммаризатора
    assert meta.content == "Ответ 6"
    assert agent.last_result.prompt_tokens == 100


def test_restart_with_fresh_summary_needs_no_summarizer(history_path):
    history = HistoryManager(path=history_path)
    first = TabletopAgent(
        FakeAgentClient(
            answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6"]
        ),
        history=history,
    )
    for i in range(1, 7):
        first.ask(f"Вопрос {i}")

    second_client = FakeAgentClient(answers=["Ответ 7"])
    second = TabletopAgent(second_client, history=HistoryManager(path=history_path))
    second.ask("Вопрос 7")

    assert len(second_client.calls) == 1  # суммаризатор не потребовался
    messages = second_client.calls[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "system", "user", "assistant", "user", "assistant", "user"]
    assert "РЕЗЮМЕ 1" in messages[1]["content"]
    contents = "".join(m["content"] for m in messages)
    assert "Вопрос 1" not in contents and "Вопрос 4" not in contents
    assert "Вопрос 5" in contents and "Вопрос 6" in contents


def test_restart_without_summary_catches_up_in_parts_at_first_question(history_path):
    """Файл без резюме (старый формат): догоняющая суммаризация при первом вопросе."""
    records = [
        {"question": f"Вопрос {i}", "answer": _padded(f"Ответ {i}")}
        for i in range(1, 13)
    ]
    history_path.write_text(json.dumps(records), encoding="utf-8")

    client = FakeAgentClient(answers=["РЕЗЮМЕ ВСЕЙ ИСТОРИИ", "Ответ 13"])
    agent = TabletopAgent(client, history=HistoryManager(path=history_path))
    agent.ask("Вопрос 13")

    assert len(client.calls) == 2  # суммаризатор + вопрос
    summary_call, question_call = client.calls[0], client.calls[1]
    summary_user = summary_call["messages"][1]["content"]
    assert "Вопрос 1" in summary_user and "Вопрос 11" in summary_user
    assert "Вопрос 12" not in summary_user  # последний обмен остаётся дословным
    assert [m["role"] for m in question_call["messages"]] == [
        "system", "system", "user", "assistant", "user",
    ]
    assert "РЕЗЮМЕ ВСЕЙ ИСТОРИИ" in question_call["messages"][1]["content"]
    assert "Вопрос 12" in question_call["messages"][2]["content"]


def test_reset_clears_stack_summary_and_file():
    agent, _ = make_agent(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6"]
    )
    for i in range(1, 6):
        agent.ask(f"Вопрос {i}")
    agent.ask("Вопрос 6")

    agent.reset()

    assert agent.stack_exchanges == 0
    assert agent._summary is None
    assert agent._summary_covers == 0
    assert json.loads(agent.history.path.read_text(encoding="utf-8"))["dialogues"] == []


def test_stack_tokens_estimate_includes_summary(history_path):
    history = HistoryManager(path=history_path)
    history.set_summary("РЕЗЮМЕ ДИАЛОГА ДЛИННОЕ ДЛИННОЕ", 0)
    agent, _ = make_agent(history=history)
    system_only = estimate_tokens(prompts.build_system_message(agent.config.format))
    assert agent.stack_tokens_estimate > system_only


def test_last_compression_is_none_before_any_digest():
    agent, _ = make_agent()
    assert agent.last_compression is None


def test_last_compression_reports_digest_size_and_skips_idle_asks():
    agent, client = make_agent(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6", "Ответ 7"]
    )
    for i in range(1, 6):
        agent.ask(f"Вопрос {i}")
    assert agent.last_compression is None  # до сжатия

    agent.ask("Вопрос 6")
    report = agent.last_compression
    assert report is not None
    assert (report.messages, report.exchanges) == (8, 4)

    agent.ask("Вопрос 7")  # порог не достигнут — сжатия нет
    assert agent.last_compression is report  # прежний отчёт уцелел


def test_phase_listener_signals_compression_then_request():
    phases = []
    agent, client = make_agent(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6"]
    )
    for i in range(1, 6):
        agent.ask(f"Вопрос {i}", on_phase=phases.append)
    agent.ask("Вопрос 6", on_phase=phases.append)

    assert phases.count(tabletop_agent.RequestPhase.REQUEST) == 6
    assert phases.count(tabletop_agent.RequestPhase.COMPRESSION) == 1
    assert client.calls[5]["messages"][0]["content"] == context_compressor.summary_instruction()


def test_default_listener_is_silent():
    agent, client = make_agent(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6"]
    )
    for i in range(1, 6):
        agent.ask(f"Вопрос {i}")  # без on_phase — никакого исключения
    agent.ask("Вопрос 6")
    assert agent.last_result.content == "Ответ 6"
