"""Фейковый MCP-сервер для тестов: отвечает по stdio, не ходит в сеть.

Запускается интерпретатором текущего прогона (`sys.executable`), поэтому не зависит ни от
`npx`, ни от установленных пакетов. Без него любой тест подключения поднимал бы настоящий
сервер из реестра — то есть лез бы в сеть.

Он же отвечает на вызовы инструментов: `fake_echo` возвращает переданные аргументы, поэтому
тест видит, что именно ушло на сервер, а неизвестное имя инструмента отдаётся ошибкой
протокола — без этого проверки вызова поднимали бы настоящий сервер реестра.

Режимы (через аргументы командной строки):

- без аргументов — объявляет три инструмента;
- ``--empty`` — объявляет пустой список инструментов (успешное подключение, но без инструментов);
- ``--garbage`` — печатает мусор вместо протокола (проверка отказа рукопожатия);
- ``--markup`` — имя и описание инструмента содержат разметкоподобные скобки (проверка
  экранирования на терминальном слое).
"""

import json
import sys

SERVER_NAME = "фейковый-сервер"
SERVER_VERSION = "9.9.9"
PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "fake_search",
        "description": "Поиск по фейковому каталогу",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "fake_details",
        "description": "Детали фейковой записи",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
    },
    {
        "name": "fake_echo",
        "description": "Возвращает переданные аргументы",
        "inputSchema": {
            "type": "object",
            "properties": {
                "first": {"type": "string", "description": "Первый аргумент"},
                "second": {"type": "string", "description": "Второй аргумент"},
            },
            "required": ["first"],
        },
    },
]

MARKUP_TOOLS = [
    {
        "name": "fake_[x]_tool",
        "description": "Описание с [/dim] разметкой",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _call_result(request_id, params, tools):
    """Ответ на tools/call: эхо аргументов для объявленного инструмента, ошибка для чужого."""
    name = params.get("name")
    if name not in {tool["name"] for tool in tools}:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32602, "message": f"инструмент {name} не объявлен"},
        }
    arguments = params.get("arguments") or {}
    pairs = ", ".join(f"{key}={value}" for key, value in sorted(arguments.items()))
    text = f"фейковый вызов {name}({pairs})"
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }


def main() -> None:
    flags = set(sys.argv[1:])

    if "--garbage" in flags:
        # Процесс запускается, но протокола не знает: клиент должен сдаться, а не ждать вечно.
        sys.stdout.write("это не JSON-RPC\n")
        sys.stdout.flush()
        return

    if "--empty" in flags:
        tools = []
    elif "--markup" in flags:
        tools = MARKUP_TOOLS
    else:
        tools = TOOLS

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            # Уведомление (notifications/initialized и подобные) — ответа не требует.
            continue

        if method == "initialize":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    },
                }
            )
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}})
        elif method == "tools/call":
            _send(_call_result(request_id, message.get("params") or {}, tools))
        else:
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "метод не поддержан"},
                }
            )


if __name__ == "__main__":
    main()
