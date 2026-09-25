"""Выбор инструмента моделью: каталог для запроса, разбор ответа, сообщение с результатом.

Чистый слой без сети и без модели — как `context_strategies` для фактов.
"""

from core import config, mcp_tools
from core.mcp_client import MCPTool
from core.tabletop_agent import MCPReport


def report(*tools: MCPTool, name: str = "сервер", error: str = "") -> MCPReport:
    return MCPReport(
        spec_name=name,
        command="команда",
        server_name=name,
        server_version="1.0",
        protocol_version="2025-06-18",
        tools=tuple(tools),
        error=error,
    )


ECHO = MCPTool(
    name="echo_tool",
    description="Возвращает переданный текст",
    input_schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Что вернуть"},
            "mode": {"type": "string", "description": "Режим", "enum": ["a", "b"], "default": "a"},
        },
        "required": ["text"],
    },
)


# --- каталог для запроса выбора ---


def test_catalog_lists_server_tool_and_parameters():
    catalog = mcp_tools.render_catalog([report(ECHO)])
    assert "сервер" in catalog
    assert "echo_tool" in catalog
    assert "text" in catalog
    assert "mode" in catalog


def test_catalog_marks_required_and_allowed_values():
    catalog = mcp_tools.render_catalog([report(ECHO)])
    assert "обязательный" in catalog
    assert "a, b" in catalog


def test_catalog_skips_servers_that_failed():
    catalog = mcp_tools.render_catalog([report(ECHO, error="не поднялся")])
    assert "echo_tool" not in catalog


def test_choice_messages_carry_instruction_and_question():
    messages = mcp_tools.build_choice_messages([report(ECHO)], "сколько хитов у гоблина?")
    assert messages[0]["role"] == "system"
    assert "JSON" in messages[0]["content"]
    assert "гоблина" in messages[1]["content"]
    assert "echo_tool" in messages[1]["content"]


# --- разбор ответа модели ---


def test_parse_choice_reads_server_tool_and_arguments():
    choice = mcp_tools.parse_choice(
        '{"server": "сервер", "tool": "echo_tool", "arguments": {"text": "привет"}}'
    )
    assert choice.server == "сервер"
    assert choice.tool == "echo_tool"
    assert choice.arguments == {"text": "привет"}


def test_parse_choice_reads_fenced_json():
    choice = mcp_tools.parse_choice('Думаю так:\n```json\n{"tool": "echo_tool"}\n```')
    assert choice.tool == "echo_tool"
    assert choice.arguments == {}


def test_parse_choice_without_tool_means_no_tool_needed():
    assert mcp_tools.parse_choice('{"tool": null}') is None
    assert mcp_tools.parse_choice("{}") is None
    assert mcp_tools.parse_choice('{"tool": ""}') is None


def test_parse_choice_of_garbage_is_a_failure():
    """Мусор и пустой ответ — не «инструмент не нужен», а сбой разбора."""
    assert mcp_tools.parse_choice("совершенно не JSON") is mcp_tools.UNPARSED
    assert mcp_tools.parse_choice("") is mcp_tools.UNPARSED


def test_parse_choice_ignores_non_object_arguments():
    choice = mcp_tools.parse_choice('{"tool": "echo_tool", "arguments": "строка"}')
    assert choice.arguments == {}


# --- сообщение с результатом ---


def test_result_message_carries_tool_name_and_text():
    message = mcp_tools.tool_result_message("сервер", "echo_tool", {"text": "привет"}, "ответ")
    assert "echo_tool" in message
    assert "ответ" in message
    assert "text=привет" in message


def test_result_message_carries_instruction():
    message = mcp_tools.tool_result_message("сервер", "echo_tool", {}, "ответ")
    assert len(message) > len("ответ") + 50


def test_render_arguments_is_readable():
    assert mcp_tools.render_arguments({"b": 2, "a": "раз"}) == "a=раз, b=2"
    assert mcp_tools.render_arguments({}) == "без аргументов"


# --- цепочка шагов (день 19) ---


CHAIN = (
    '{"steps": ['
    '{"tool": "fake_search", "arguments": {"query": "огонь"}},'
    '{"server": "сервер", "tool": "echo_tool", "arguments": {"text": "$1"}}'
    "]}"
)


def test_parse_chain_reads_steps_in_order():
    steps, dropped = mcp_tools.parse_chain(CHAIN)
    assert [step.tool for step in steps] == ["fake_search", "echo_tool"]
    assert steps[0].server == ""
    assert steps[1].server == "сервер"
    assert steps[1].arguments == {"text": "$1"}
    assert dropped == 0


def test_parse_chain_accepts_the_single_tool_format():
    """Прежний ответ с одним инструментом — цепочка из одного шага."""
    steps, dropped = mcp_tools.parse_chain('{"tool": "echo_tool", "arguments": {"text": "a"}}')
    assert [step.tool for step in steps] == ["echo_tool"]
    assert dropped == 0


def test_parse_chain_no_tool_is_none():
    assert mcp_tools.parse_chain('{"tool": null}') is None
    assert mcp_tools.parse_chain('{"steps": []}') is None


def test_parse_chain_unparsed_is_a_failure():
    assert mcp_tools.parse_chain("совсем не JSON") is mcp_tools.UNPARSED


def test_parse_chain_caps_the_number_of_steps():
    steps = ",".join('{"tool": "echo_tool", "arguments": {}}' for _ in range(config.TOOL_CHAIN_MAX_STEPS + 2))
    parsed, dropped = mcp_tools.parse_chain('{"steps": [' + steps + "]}")
    assert len(parsed) == config.TOOL_CHAIN_MAX_STEPS == 4
    assert dropped == 2


def test_parse_chain_skips_steps_without_tool_name():
    steps, _ = mcp_tools.parse_chain('{"steps": [{"tool": ""}, {"tool": "echo_tool"}, "мусор"]}')
    assert [step.tool for step in steps] == ["echo_tool"]


def test_reference_is_replaced_verbatim():
    text = "строка 1\nстрока 2 с «кавычками» и $1 внутри"
    resolved, sources = mcp_tools.resolve_references({"text": "$1", "name": "файл"}, [text])
    assert resolved == {"text": text, "name": "файл"}
    assert sources == {"text": 1}


def test_reference_inside_other_text_is_left_as_is():
    resolved, sources = mcp_tools.resolve_references({"text": "см. $1"}, ["данные"])
    assert resolved == {"text": "см. $1"}
    assert sources == {}


def test_reference_to_unfinished_step_is_an_error():
    import pytest

    with pytest.raises(mcp_tools.ReferenceFailure, match="шаг 2"):
        mcp_tools.resolve_references({"text": "$2"}, ["только первый"])
    with pytest.raises(mcp_tools.ReferenceFailure):
        mcp_tools.resolve_references({"text": "$0"}, ["первый"])


def test_arguments_render_references_as_step_links():
    rendered = mcp_tools.render_arguments({"text": "очень длинный текст", "name": "x"}, {"text": 1})
    assert rendered == "name=x, text=← шаг 1"


def test_chain_message_names_every_step_without_repeating_passed_text():
    steps = [
        ("сервер", "fake_search", {"query": "огонь"}, {}, "НАЙДЕНО-ТЕКСТ"),
        ("сервер", "echo_tool", {"text": "НАЙДЕНО-ТЕКСТ"}, {"text": 1}, "СВОДКА-ТЕКСТ"),
    ]
    message = mcp_tools.tool_chain_message(steps)
    assert "Шаг 1" in message and "Шаг 2" in message
    assert "fake_search" in message and "echo_tool" in message
    assert "text=← шаг 1" in message
    # Переданный текст в сообщении один раз — как результат шага 1, а не ещё и в аргументах.
    assert message.count("НАЙДЕНО-ТЕКСТ") == 1
    assert "СВОДКА-ТЕКСТ" in message


# --- маршрутизация шага по каталогу (день 20) ---


OTHER = MCPTool(name="other_tool", description="Другой инструмент", input_schema={})


def two_servers():
    return [report(ECHO, name="первый"), report(OTHER, name="второй")]


def test_route_keeps_the_named_server_that_declares_the_tool():
    route = mcp_tools.route(two_servers(), "второй", "other_tool")
    assert (route.server, route.rerouted_from, route.error) == ("второй", "", "")


def test_route_finds_the_server_when_none_is_named():
    route = mcp_tools.route(two_servers(), "", "echo_tool")
    assert (route.server, route.rerouted_from, route.error) == ("первый", "", "")


def test_route_fixes_a_wrong_server_and_remembers_it():
    route = mcp_tools.route(two_servers(), "второй", "echo_tool")
    assert (route.server, route.rerouted_from, route.error) == ("первый", "второй", "")


def test_route_rejects_a_tool_no_server_declares():
    route = mcp_tools.route(two_servers(), "первый", "нет_такого")
    assert route.server == ""
    assert "нет_такого" in route.error and "ни одним" in route.error


def test_route_rejects_an_ambiguous_tool_without_a_matching_server():
    reports = [report(ECHO, name="первый"), report(ECHO, name="второй"), report(OTHER, name="третий")]
    route = mcp_tools.route(reports, "третий", "echo_tool")
    assert route.server == ""
    assert "неоднозначно" in route.error
    assert "первый" in route.error and "второй" in route.error


def test_route_accepts_an_ambiguous_tool_when_the_server_is_named():
    reports = [report(ECHO, name="первый"), report(ECHO, name="второй")]
    route = mcp_tools.route(reports, "второй", "echo_tool")
    assert (route.server, route.error) == ("второй", "")


def test_route_skips_servers_that_failed():
    reports = [report(ECHO, name="первый", error="не поднялся"), report(OTHER, name="второй")]
    assert mcp_tools.route(reports, "", "echo_tool").error


# --- флоу из раундов (день 20) ---


def test_round_without_more_ends_the_flow():
    parsed = mcp_tools.parse_round('{"steps": [{"tool": "echo_tool", "arguments": {"text": "a"}}]}')
    assert [step.tool for step in parsed.steps] == ["echo_tool"]
    assert parsed.more is False
    assert parsed.dropped == 0


def test_round_with_more_asks_for_the_next_round():
    parsed = mcp_tools.parse_round('{"steps": [{"tool": "echo_tool", "arguments": {}}], "more": true}')
    assert parsed.more is True


def test_single_tool_with_more():
    parsed = mcp_tools.parse_round('{"tool": "echo_tool", "arguments": {}, "more": true}')
    assert [step.tool for step in parsed.steps] == ["echo_tool"]
    assert parsed.more is True


def test_done_null_tool_and_empty_steps_end_the_flow():
    assert mcp_tools.parse_round('{"done": true}') is None
    assert mcp_tools.parse_round('{"tool": null}') is None
    assert mcp_tools.parse_round('{"steps": [], "more": true}') is None


def test_unparsable_round_is_a_failure():
    assert mcp_tools.parse_round("не JSON") is mcp_tools.UNPARSED


def test_parse_chain_keeps_its_old_shape():
    steps, dropped = mcp_tools.parse_chain('{"steps": [{"tool": "echo_tool"}], "more": true}')
    assert [step.tool for step in steps] == ["echo_tool"] and dropped == 0


class Step:
    """Выполненный шаг в том виде, в каком его видит запрос раунда."""

    def __init__(self, step, text, server="первый", tool="echo_tool", arguments=None, sources=None, round=1):
        self.step, self.text, self.server, self.tool, self.round = step, text, server, tool, round
        self.arguments = arguments or {"text": "x"}
        self.sources = sources or {}


def test_round_messages_carry_question_catalog_and_done_steps():
    done = [Step(1, "результат первого"), Step(2, "результат второго", server="второй", tool="other_tool")]
    messages = mcp_tools.build_round_messages(two_servers(), "вопрос про гоблина", done)
    system, user = messages
    assert system["role"] == "system" and "done" in system["content"]
    content = user["content"]
    assert "вопрос про гоблина" in content
    assert "echo_tool" in content and "other_tool" in content
    assert "Шаг 1" in content and "результат первого" in content
    assert "Шаг 2" in content and "второй" in content and "результат второго" in content
    # Номер следующего шага назван явно: ссылки $N сквозные через раунды.
    assert "3" in content.split("Шаг 2")[-1]


def test_round_messages_keep_the_newest_result_whole(monkeypatch):
    monkeypatch.setattr(config, "TOOL_FLOW_CONTEXT_CHARS", 100)
    done = [Step(1, "с" * 80), Step(2, "н" * 90)]
    content = mcp_tools.build_round_messages(two_servers(), "вопрос", done)[1]["content"]
    assert "н" * 90 in content
    assert "с" * 11 not in content
    assert "сокращено" in content and "80" in content


def test_round_messages_shorten_results_of_earlier_rounds(monkeypatch):
    """Прошлые раунды — справка, свежий — рабочий материал: рулбук после отправки сводки не нужен."""
    monkeypatch.setattr(config, "TOOL_FLOW_OLD_RESULT_CHARS", 30)
    done = [Step(1, "р" * 500, round=1), Step(2, "с" * 200, round=2), Step(3, "н" * 200, round=2)]
    content = mcp_tools.build_round_messages(two_servers(), "вопрос", done)[1]["content"]
    assert "р" * 31 not in content and "показано 30 из 500" in content
    assert "с" * 200 in content and "н" * 200 in content


def test_round_messages_show_references_as_step_links():
    done = [Step(1, "текст"), Step(2, "второй", arguments={"text": "текст"}, sources={"text": 1})]
    content = mcp_tools.build_round_messages(two_servers(), "вопрос", done)[1]["content"]
    assert "text=← шаг 1" in content


def test_flow_limits_are_configuration():
    assert config.TOOL_FLOW_MAX_ROUNDS == 6
    assert config.TOOL_FLOW_MAX_STEPS == 10
    assert config.TOOL_FLOW_CONTEXT_CHARS == 24000
    assert config.TOOL_FLOW_OLD_RESULT_CHARS == 1500
    # Длинный флоу на живой модели не укладывался ни в 2000, ни в 4050 токенов выбора (день 20).
    assert config.TOOL_CHOICE_MAX_WORDS == 2000


def test_choice_instruction_explains_more_and_tool_text_as_data():
    instruction = mcp_tools.choice_instruction()
    assert '"more": true' in instruction
    assert "данные" in instruction
