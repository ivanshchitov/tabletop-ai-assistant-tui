"""Собственный MCP-сервер проекта: справочник правил D&D 5e по транспорту stdio.

Запускается как отдельный процесс — так же, как чужие серверы реестра: приложение знает о нём
только команду запуска из `core.config.MCP_SERVERS`, а имена инструментов, их описания и схемы
входа приходят по протоколу. Поэтому замена сервера остаётся правкой одной записи реестра.

Объявление инструментов и выполнение вызовов живут в `dnd_tools.py`: здесь остаётся только
протокол, что и позволяет проверять инструменты юнит-тестами без запуска процесса.

Запуск: `python mcp_server/dnd_server.py`. Базовый адрес внешнего API берётся из переменной
окружения `TABLETOP_DND_API_URL` (см. `dnd_api.py`).
"""

import asyncio
import sys
from pathlib import Path
from typing import Any

# Скрипт запускают файлом, а не как модуль пакета, поэтому корень репозитория добавляется в
# путь импорта вручную — иначе относительный импорт `.dnd_tools` не разрешится.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server.dnd_tools import TOOLS, DndTools  # noqa: E402

SERVER_NAME = "dnd-rules"
SERVER_VERSION = "1.0.0"
SERVER_INSTRUCTIONS = (
    "Справочник правил настольной ролевой игры D&D 5e: разделы, поиск по разделу и полная "
    "запись по идентификатору."
)


def build_server(tools: DndTools):
    """Собрать сервер с тремя инструментами поверх готового исполнителя вызовов."""
    from mcp import types
    from mcp.server.lowlevel import Server

    declarations = [
        types.Tool(name=tool.name, description=tool.description, inputSchema=tool.input_schema)
        for tool in TOOLS
    ]

    async def on_list_tools(context, params) -> Any:
        return types.ListToolsResult(tools=declarations)

    async def on_call_tool(context, params) -> Any:
        # Вызов ходит в сеть, поэтому выполняется в отдельном потоке: блокирующий запрос
        # прямо в событийном цикле задержал бы и ответ на ping, и завершение процесса.
        text = await asyncio.to_thread(tools.call, params.name, dict(params.arguments or {}))
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def serve() -> None:
    from mcp.server.stdio import stdio_server

    server = build_server(DndTools())
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
