"""Выбор инструмента моделью: каталог для запроса, разбор ответа, сообщение с результатом.

Чистый слой без сети и без модели — как `context_strategies` для фактов.
"""

from core import mcp_tools
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
