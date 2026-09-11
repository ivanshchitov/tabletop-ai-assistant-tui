"""Агент: память диалога, стратегии контекста и пересылка LLM."""

import json
from typing import List, Optional

import pytest
from core import config, context_compressor, context_strategies, prompts, tabletop_agent
from core import context_strategies as strategies
from core.usage import estimate_tokens
from core.answer_settings import AnswerFormat, AnswerSettings, ContextStrategy
from core.api_client import AnswerMeta, APIError
from core.history_manager import HistoryManager
from core.tabletop_agent import TabletopAgent


class FakeAgentClient:
    """Подставной клиент в терминах сообщений: запоминает полный список messages.

    temperature=None означает «вызывающий не передал температуру» — так отличают
    вспомогательный запрос стратегии (клиентский дефолт) от явной передачи настройки.
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
    report = agent.context_report()
    assert report.summary_covers == 4
    assert report.log_exchanges == 6  # свёрнутые обмены остались в логе


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
    assert agent.context_report().log_exchanges == 5
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
        "facts": {},
        "dialogues": [],
    }
    assert agent.history.dialogues == []

    agent.ask("Вопрос после очистки")
    messages = client.calls[-1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert "Вопрос до очистки" not in messages[1]["content"]



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

def test_context_tokens_estimate_counts_system_message():
    agent, _ = make_agent()
    # оценка собираемого запроса без ходов — системное сообщение (плюс пустой вопрос)
    assert agent.context_report().tokens_estimate >= estimate_tokens(
        prompts.build_system_message(agent.config.format)
    )


def test_context_tokens_estimate_grows_with_turns():
    agent, _ = make_agent()
    before = agent.context_report().tokens_estimate
    agent.ask("Вопрос")
    assert agent.context_report().tokens_estimate > before




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


def test_context_tokens_estimate_collapses_after_digest():
    """Оценка запроса после сжатия падает: резюме + хвост вместо десяти дословных ходов."""
    agent, client = make_agent(
        answers=[f"Ответ {i} " + "х" * 700 for i in range(1, 6)]
        + ["РЕЗЮМЕ 1", "Ответ 6"]
    )
    for i in range(1, 6):
        agent.ask(f"Вопрос {i}")
    before = agent.context_report().tokens_estimate
    agent.ask("Вопрос 6")
    assert agent.context_report().tokens_estimate < before


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

    assert agent.context_report().log_exchanges == 0
    assert agent._summary is None
    assert agent._summary_covers == 0
    assert json.loads(agent.history.path.read_text(encoding="utf-8"))["dialogues"] == []


def test_context_tokens_estimate_includes_summary(history_path):
    history = HistoryManager(path=history_path)
    history.set_summary("РЕЗЮМЕ ДИАЛОГА ДЛИННОЕ ДЛИННОЕ", 0)
    agent, _ = make_agent(history=history)
    system_only = estimate_tokens(prompts.build_system_message(agent.config.format))
    assert agent.context_report().tokens_estimate > system_only


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


# --- день 10: стратегии управления контекстом -------------------------------------------


class StrategyClient(FakeAgentClient):
    """Клиент, различающий роль запроса: извлекатель фактов, суммаризатор, вопрос.

    Стратегии шлют вспомогательные запросы помимо вопроса, поэтому ответы выбираются по
    системному сообщению, а не по порядку вызовов: тест тогда проверяет, что именно ушло
    модели, а не сколько раз её дёрнули.
    """

    def __init__(self, answers=None, facts_answers=None, summary_answers=None) -> None:
        super().__init__(answers=answers)
        # Строка — ответ извлекателя, Exception — сбой этого запроса.
        self.facts_answers = list(facts_answers or ["{}"])
        self.summary_answers = list(summary_answers or [])
        self.facts_prompts: List[str] = []
        self.summary_prompts: List[str] = []
        self.question_messages: List[List[dict]] = []

    def ask_with_usage_messages(self, messages, **kwargs):
        system = messages[0]["content"]
        if system == context_strategies.facts_instruction():
            self.facts_prompts.append(messages[1]["content"])
            answer = self._next(self.facts_answers)
            if isinstance(answer, Exception):
                raise answer
            return self._meta(answer, kwargs.get("model"))
        if system == context_compressor.summary_instruction():
            self.summary_prompts.append(messages[1]["content"])
            if not self.summary_answers:
                raise APIError("Суммаризатор не должен вызываться на этой стратегии.")
            return self._meta(self._next(self.summary_answers), kwargs.get("model"))
        self.question_messages.append([dict(message) for message in messages])
        return super().ask_with_usage_messages(messages, **kwargs)

    @staticmethod
    def _next(items):
        return items.pop(0) if len(items) > 1 else items[0]

    def _meta(self, content, model) -> AnswerMeta:
        return AnswerMeta(
            content=content,
            model=model or config.DEFAULT_MODEL,
            elapsed_seconds=0.01,
            prompt_tokens=5,
            completion_tokens=5,
            total_tokens=10,
            cost_usd=0.00001,
        )


def with_strategy(agent, strategy, **settings):
    """Переключает стратегию и настройки сессии так, как это сделал бы экран /settings."""
    updated = agent.settings.with_context_strategy(strategy)
    for name, value in settings.items():
        updated = getattr(updated, f"with_{name}")(value)
    agent.settings = updated


def strategy_agent(**kwargs):
    """Агент с клиентом, различающим роль запроса; возвращает (агент, клиент)."""
    client = StrategyClient(**kwargs)
    agent, _ = make_agent(client=client)
    return agent, client


def roles(messages):
    return [message["role"] for message in messages]


def test_digested_turns_stay_in_the_log():
    """Сжатие больше не выбрасывает ходы: они остаются в логе сессии."""
    agent, client = make_agent(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6"]
    )
    for i in range(1, 6):
        agent.ask(f"Вопрос {i}")
    agent.ask("Вопрос 6")

    assert agent.context_report().log_exchanges == 6
    assert agent.context_report().summary_covers == 4


def test_switching_to_window_brings_digested_turns_back():
    """Резюме лишь накрывает префикс лога: стратегия окна снова отправляет эти ходы."""
    agent, client = make_agent(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "РЕЗЮМЕ 1", "Ответ 6", "Ответ 7"]
    )
    for i in range(1, 6):
        agent.ask(f"Вопрос {i}")
    agent.ask("Вопрос 6")  # сжатие свернуло обмены 1-4

    with_strategy(agent, ContextStrategy.SLIDING_WINDOW, compress_after=config.MAX_COMPRESS_AFTER)
    agent.ask("Вопрос 7")

    messages = client.calls[-1]["messages"]
    contents = "".join(message["content"] for message in messages)
    assert roles(messages)[0] == "system"
    assert "РЕЗЮМЕ 1" not in contents  # окно на резюме не смотрит
    assert "Вопрос 1" in contents  # свёрнутый ход вернулся дословно
    assert "Вопрос 6" in contents and "Вопрос 7" in contents


def test_window_strategy_sends_only_the_last_messages_and_no_summarizer():
    agent, client = strategy_agent(
        answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4", "Ответ 5", "Ответ 6"]
    )
    with_strategy(agent, ContextStrategy.SLIDING_WINDOW, compress_after=5)
    for i in range(1, 7):
        agent.ask(f"Вопрос {i}")

    messages = client.question_messages[-1]
    # окно 5 сообщений — целыми обменами это последние два обмена плюс новый user-ход
    assert roles(messages) == ["system", "user", "assistant", "user", "assistant", "user"]
    contents = "".join(message["content"] for message in messages)
    assert "Вопрос 3" not in contents
    assert "Вопрос 4" in contents and "Вопрос 5" in contents and "Вопрос 6" in contents
    assert client.summary_prompts == []  # окно суммаризатор не зовёт


def test_window_over_the_ceiling_drops_oldest_exchanges_but_keeps_the_last():
    agent, client = strategy_agent(answers=["Ответ 1", "Ответ 2", "Ответ 3", "Ответ 4"])
    with_strategy(agent, ContextStrategy.SLIDING_WINDOW, compress_after=8, max_session_tokens=5000)
    for i in range(1, 5):
        agent.ask(f"Вопрос {i}" if i < 4 else "Вопрос 4 " + "о" * 20000)

    messages = client.question_messages[-1]
    contents = "".join(message["content"] for message in messages)
    assert "Вопрос 1" not in contents and "Вопрос 2" not in contents
    assert "Вопрос 3" in contents
    assert roles(messages)[-2:] == ["assistant", "user"]
    assert client.summary_prompts == []  # потолок не включает суммаризатор


def test_facts_strategy_updates_the_block_before_the_question():
    agent, client = strategy_agent(
        answers=["Ответ 1"],
        facts_answers=['{"цель": "собрать ТЗ по Каркассону"}'],
    )
    with_strategy(agent, ContextStrategy.STICKY_FACTS)

    agent.ask("Помоги собрать ТЗ по Каркассону")

    assert len(client.facts_prompts) == 1
    assert "Помоги собрать ТЗ по Каркассону" in client.facts_prompts[0]
    messages = client.question_messages[0]
    assert roles(messages) == ["system", "system", "user"]
    assert "цель: собрать ТЗ по Каркассону" in messages[1]["content"]
    assert "Помоги собрать ТЗ по Каркассону" in messages[2]["content"]


def test_facts_block_replaces_the_value_of_a_known_key():
    agent, client = strategy_agent(
        answers=["Ответ 1", "Ответ 2"],
        facts_answers=['{"цель": "собираем ТЗ"}', '{"цель": "собираем ТЗ по игре"}'],
    )
    with_strategy(agent, ContextStrategy.STICKY_FACTS)

    agent.ask("Первый вопрос")
    agent.ask("Теперь конкретнее по игре")

    block = client.question_messages[1][1]["content"]
    assert "цель: собираем ТЗ по игре" in block
    assert block.count("цель:") == 1
    assert "собираем ТЗ\n" not in block


def test_facts_only_pending_messages_go_to_the_next_update():
    agent, client = strategy_agent(
        answers=["Ответ 1", "Ответ 2"],
        facts_answers=['{"цель": "ТЗ"}', "{}"],
    )
    with_strategy(agent, ContextStrategy.STICKY_FACTS)

    agent.ask("Первый вопрос")
    agent.ask("Второй вопрос")

    second_prompt = client.facts_prompts[1]
    assert "цель: ТЗ" in second_prompt  # текущий блок передан целиком
    assert "Первый вопрос" not in second_prompt  # уже переработанное не повторяется
    assert "Второй вопрос" in second_prompt


def test_facts_extractor_failure_keeps_the_answer_and_repends_the_message():
    agent, client = strategy_agent(
        answers=["Ответ 1", "Ответ 2"],
        facts_answers=[APIError("Извлекатель упал."), '{"цель": "ТЗ"}'],
    )
    with_strategy(agent, ContextStrategy.STICKY_FACTS)

    meta = agent.ask("Первый вопрос")

    assert meta.content == "Ответ 1"  # сбой вспомогательного запроса не отменяет ответ
    report = agent.last_facts
    assert report.updated is False
    assert "Извлекатель упал." in report.error

    agent.ask("Второй вопрос")
    assert "Первый вопрос" in client.facts_prompts[1]  # накопленное ушло со следующей попыткой
    assert "Второй вопрос" in client.facts_prompts[1]
    assert agent.last_facts.updated is True
    assert agent.last_facts.keys == 1


def test_facts_extractor_non_json_is_a_failure_not_an_empty_block():
    agent, client = strategy_agent(
        answers=["Ответ 1"],
        facts_answers=["Фактов не нашёл."],
    )
    with_strategy(agent, ContextStrategy.STICKY_FACTS)

    agent.ask("Первый вопрос")

    assert agent.last_facts.updated is False
    assert agent.context_report().facts == {}
    assert roles(client.question_messages[0]) == ["system", "user"]  # пустой блок не отправляется


def test_facts_empty_object_clears_the_queue():
    agent, client = strategy_agent(
        answers=["Ответ 1", "Ответ 2"],
        facts_answers=["{}", '{"ограничение": "не более 10 сообщений"}'],
    )
    with_strategy(agent, ContextStrategy.STICKY_FACTS)

    agent.ask("Первый вопрос")
    agent.ask("Второй вопрос")

    assert "Первый вопрос" not in client.facts_prompts[1]  # {} — успешное обновление, не сбой


def test_facts_are_saved_to_history_and_restored():
    agent, client = strategy_agent(
        answers=["Ответ 1"],
        facts_answers=['{"цель": "ТЗ"}'],
    )
    with_strategy(agent, ContextStrategy.STICKY_FACTS)
    agent.ask("Первый вопрос")

    saved = json.loads(agent.history.path.read_text(encoding="utf-8"))
    assert saved["facts"] == {"цель": "ТЗ"}

    restored, restored_client = make_agent(history=agent.history, answers=["Ответ 2"])
    restored.settings = restored.settings.with_context_strategy(ContextStrategy.STICKY_FACTS)
    restored.ask("Второй вопрос")

    messages = restored_client.calls[0]["messages"]
    assert "цель: ТЗ" in messages[1]["content"]


def test_facts_spend_and_phase_are_reported():
    phases = []
    agent, client = strategy_agent(
        answers=["Ответ 1"],
        facts_answers=['{"цель": "ТЗ"}'],
    )
    with_strategy(agent, ContextStrategy.STICKY_FACTS)

    agent.ask("Первый вопрос", on_phase=phases.append)

    assert phases == [
        tabletop_agent.RequestPhase.FACTS_UPDATE,
        tabletop_agent.RequestPhase.REQUEST,
    ]
    assert agent.session_usage.requests == 2  # извлекатель плюс вопрос
    assert agent.last_result.content == "Ответ 1"


def test_branching_strategy_sends_only_the_active_branch():
    agent, client = strategy_agent(answers=["Ответ 1", "Ответ 2", "Ответ 3"])
    with_strategy(agent, ContextStrategy.BRANCHING)
    agent.ask("Вопрос ветки 1")
    agent.checkpoint()
    agent.new_branch()

    agent.ask("Вопрос ветки 2")

    messages = client.question_messages[-1]
    contents = "".join(message["content"] for message in messages)
    assert "Вопрос ветки 2" in contents
    assert "Вопрос ветки 1" in contents  # ветка скопировала ходы до чекпоинта
    assert "Ответ 1" in contents

    assert agent.switch_branch("ветка 1") is True
    agent.ask("Вопрос обратно в ветке 1")
    contents = "".join(m["content"] for m in client.question_messages[-1])
    assert "Вопрос ветки 2" not in contents  # обмен второй ветки остался в ней


def test_switch_to_unknown_branch_reports_failure_and_keeps_the_active_one():
    agent, _ = make_agent()
    with_strategy(agent, ContextStrategy.BRANCHING)

    assert agent.switch_branch("нет такой ветки") is False
    assert agent.context_report().branch == strategies.DEFAULT_BRANCH_NAME


def test_context_report_describes_the_active_strategy():
    agent, client = make_agent(answers=["Ответ 1", "Ответ 2"])
    agent.ask("Первый вопрос")

    report = agent.context_report()

    assert report.strategy is ContextStrategy.SUMMARY
    assert report.window == agent.settings.compress_after
    assert report.max_session_tokens == agent.settings.max_session_tokens
    assert report.log_exchanges == 1
    assert report.request_turns == 2
    assert report.has_summary is False
    assert report.facts == {}
    assert report.branches == ((strategies.DEFAULT_BRANCH_NAME, 1),)
    assert report.tokens_estimate > 0
    assert len(client.calls) == 1  # снимок не обращается к модели


def test_context_report_carries_facts_and_branches():
    agent, client = strategy_agent(answers=["Ответ 1"], facts_answers=['{"цель": "ТЗ"}'])
    with_strategy(agent, ContextStrategy.STICKY_FACTS)
    agent.ask("Первый вопрос")
    agent.checkpoint()
    agent.new_branch()

    report = agent.context_report()

    assert report.strategy is ContextStrategy.STICKY_FACTS
    assert report.facts == {"цель": "ТЗ"}
    assert report.branch == "ветка 2"
    assert [name for name, _ in report.branches] == ["ветка 1", "ветка 2"]


def test_reset_clears_facts_and_branches():
    agent, client = strategy_agent(answers=["Ответ 1"], facts_answers=['{"цель": "ТЗ"}'])
    with_strategy(agent, ContextStrategy.STICKY_FACTS)
    agent.ask("Первый вопрос")
    agent.new_branch()

    agent.reset()

    report = agent.context_report()
    assert report.facts == {}
    assert report.branches == ((strategies.DEFAULT_BRANCH_NAME, 0),)
    assert report.log_exchanges == 0


def test_other_strategies_never_call_the_summarizer():
    """Под окном порог сжатия не имеет значения: ходы просто не попадают в запрос."""
    agent, client = strategy_agent(answers=["Ответ 1", "Ответ 2"])
    with_strategy(agent, ContextStrategy.SLIDING_WINDOW, compress_after=5)
    agent.ask("Вопрос 1")
    agent.ask("Вопрос 2")

    assert client.summary_prompts == []
    assert len(client.question_messages) == 2
