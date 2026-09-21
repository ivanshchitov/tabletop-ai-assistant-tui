"""Фейковый MCP-сервер для тестов: отвечает по stdio, не ходит в сеть.

Запускается интерпретатором текущего прогона (`sys.executable`), поэтому не зависит ни от
`npx`, ни от установленных пакетов. Без него любой тест подключения поднимал бы настоящий
сервер из реестра — то есть лез бы в сеть.

Режимы (через аргументы командной строки):

- без аргументов — объявляет два инструмента;
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
