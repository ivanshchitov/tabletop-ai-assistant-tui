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
from typing import Any, Optional

# Скрипт запускают файлом, а не как модуль пакета, поэтому корень репозитория добавляется в
# путь импорта вручную — иначе относительный импорт `.dnd_tools` не разрешится.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp_server.dnd_tools import TOOLS, DndTools  # noqa: E402
from mcp_server.scheduler import SCHEDULE_TOOL_NAMES, SCHEDULE_TOOLS, Scheduler  # noqa: E402

SERVER_NAME = "dnd-rules"
SERVER_VERSION = "1.0.0"
SERVER_INSTRUCTIONS = (
    "Справочник правил настольной ролевой игры D&D 5e: разделы, поиск по разделу, полная "
    "запись по идентификатору и сбор раздела с накоплением, плюс планировщик отложенных и "
    "периодических вызовов этих инструментов."
)


def build_server(tools: DndTools, scheduler: Optional[Scheduler] = None):
    """Собрать сервер: справочные инструменты и планировщик поверх готового исполнителя вызовов."""
    from mcp import types
    from mcp.server.lowlevel import Server

    if scheduler is None:
        # Планировщик зовёт инструменты этого же сервера, внутри процесса: кросс-серверный
        # вызов потребовал бы второго MCP-клиента внутри сервера.
        scheduler = Scheduler(
            store=tools.store,
            call_tool=tools.call_result,
            tool_names=[tool.name for tool in TOOLS],
        )

    declarations = [
        types.Tool(name=tool.name, description=tool.description, inputSchema=tool.input_schema)
        for tool in TOOLS + SCHEDULE_TOOLS
    ]

    async def on_list_tools(context, params) -> Any:
        return types.ListToolsResult(tools=declarations)

    async def on_call_tool(context, params) -> Any:
        # Вызов ходит в сеть (а вызов планировщика — ещё и в инструменты), поэтому выполняется
        # в отдельном потоке: блокирующий запрос прямо в событийном цикле задержал бы и ответ
        # на ping, и завершение процесса.
        call = scheduler.call if params.name in SCHEDULE_TOOL_NAMES else tools.call
        text = await asyncio.to_thread(call, params.name, dict(params.arguments or {}))
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
