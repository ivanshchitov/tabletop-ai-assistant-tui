"""Клиент MCP: соединение по stdio, рукопожатие, список инструментов и вызов инструмента.

Приложение синхронное (главный цикл — обычный ``input()``), а официальный пакет ``mcp``
асинхронный. Граница двух миров спрятана здесь: наружу модуль отдаёт обычные синхронные
методы, а ``asyncio.run`` живёт внутри. Перевод всего приложения на асинхронность стоил бы
правки всего ``ui/`` и конвейера задач ради одной команды.

Соединение поднимается по требованию и закрывается сразу после получения данных: держать
процесс сервера всю сессию означало бы фоновый поток с собственным событийным циклом и
остановку процесса при выходе — для команды-отчёта это лишняя сложность.

Модуль ничего не знает о конкретном сервере: имена инструментов, их описания и схемы приходят
от сервера. Именно это делает замену сервера одной записью реестра `config.MCP_SERVERS`.
"""

import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import config


class MCPError(Exception):
    """Сбой подключения к MCP-серверу: процесс не запустился, рукопожатие не прошло, нет ответа."""


@dataclass(frozen=True)
class MCPTool:
    """Инструмент так, как его объявил сервер: имя, описание и схема входных параметров."""

    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)

    def parameters(self) -> List["MCPParameter"]:
        """Параметры схемы в удобном для отчёта виде; чужая схема может быть какой угодно."""
        properties = self.input_schema.get("properties") if isinstance(self.input_schema, dict) else None
        if not isinstance(properties, dict):
            return []
        required = self.input_schema.get("required") or []
        parameters = []
        for name, definition in properties.items():
            definition = definition if isinstance(definition, dict) else {}
            parameters.append(
                MCPParameter(
                    name=str(name),
                    type=str(definition.get("type", "")),
                    description=str(definition.get("description", "")),
                    required=name in required,
                    allowed=tuple(str(value) for value in definition.get("enum", []) or []),
                    default=None if definition.get("default") is None else str(definition["default"]),
                )
            )
        return parameters


@dataclass(frozen=True)
class MCPParameter:
    """Один входной параметр инструмента — для отчёта и для запроса выбора инструмента."""

    name: str
    type: str
    description: str
    required: bool
    allowed: tuple = ()
    default: Optional[str] = None


@dataclass(frozen=True)
class MCPConnection:
    """Результат успешного подключения."""

    server_name: str
    server_version: str
    protocol_version: str
    tools: List[MCPTool]


class MCPClient:
    """Синхронная обёртка над асинхронным SDK для одного описания сервера."""

    def __init__(self, spec: Optional[config.MCPServerSpec] = None, timeout: float = 60.0) -> None:
        self.spec = spec if spec is not None else config.mcp_server_spec()
        self.timeout = timeout

    @property
    def command_line(self) -> str:
        """Команда запуска строкой — для отчёта."""
        return " ".join([self.spec.command, *self.spec.args])

    def server_environment(self) -> Dict[str, str]:
        """Переменные с секретами сервера, которые есть в окружении.

        Отсутствующая переменная просто не передаётся: описание перечисляет, что сервер умеет
        принять, а не что обязано быть задано.
        """
        values = {}
        for key in self.spec.env_keys:
            value = os.getenv(key)
            if value:
                values[key] = value
        return values

    def connect(self) -> MCPConnection:
        """Поднять соединение, выполнить рукопожатие и получить список инструментов.

        Любой сбой (нет команды, процесс не говорит по протоколу, таймаут) превращается в
        `MCPError`: вызывающая сторона показывает текст пользователю, а сессия продолжается.
        """
        if self.spec.transport != "stdio":
            raise MCPError(f"Транспорт «{self.spec.transport}» не поддерживается")
        _require_sdk()
        try:
            return asyncio.run(self._connect())
        except MCPError:
            raise
        except asyncio.TimeoutError as error:
            raise MCPError(f"Сервер не ответил за {self.timeout:.0f} с") from error
        except FileNotFoundError as error:
            raise MCPError(f"Команда запуска не найдена: {self.spec.command}") from error
        except BaseException as error:  # noqa: BLE001
            # SDK заворачивает ошибки задач в ExceptionGroup (BaseException), поэтому ловим широко:
            # ни один сбой чужого процесса не должен уронить сессию.
            raise MCPError(_describe(error)) from error

    async def _connect(self) -> MCPConnection:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        parameters = StdioServerParameters(
            command=self.spec.command,
            args=list(self.spec.args),
            env=self.server_environment() or None,
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await asyncio.wait_for(session.initialize(), self.timeout)
                listing = await asyncio.wait_for(session.list_tools(), self.timeout)

        info = initialized.server_info
        tools = [
            MCPTool(
                name=tool.name,
                description=tool.description or "",
                # mcp 2.x отдаёт поля в snake_case — как server_info и protocol_version выше.
                input_schema=dict(tool.input_schema or {}),
            )
            for tool in listing.tools
        ]
        return MCPConnection(
            server_name=info.name,
            server_version=info.version or "",
            protocol_version=initialized.protocol_version,
            tools=tools,
        )


    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """Вызвать инструмент сервера и вернуть текст результата.

        Соединение одноразовое, как у `connect()`: процесс поднимается на вызов и гасится
        сразу после ответа. Любой сбой — отсутствие команды, отказ рукопожатия, неизвестный
        инструмент, ошибка самого инструмента — приходит как `MCPError`, чтобы вызывающая
        сторона превратила его в данные снимка, а не в падение сессии.
        """
        if self.spec.transport != "stdio":
            raise MCPError(f"Транспорт «{self.spec.transport}» не поддерживается")
        _require_sdk()
        try:
            return asyncio.run(self._call_tool(name, arguments or {}))
        except MCPError:
            raise
        except asyncio.TimeoutError as error:
            raise MCPError(f"Сервер не ответил за {self.timeout:.0f} с") from error
        except FileNotFoundError as error:
            raise MCPError(f"Команда запуска не найдена: {self.spec.command}") from error
        except BaseException as error:  # noqa: BLE001
            raise MCPError(_describe(error)) from error

    async def _call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        parameters = StdioServerParameters(
            command=self.spec.command,
            args=list(self.spec.args),
            env=self.server_environment() or None,
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), self.timeout)
                result = await asyncio.wait_for(
                    session.call_tool(name, arguments), self.timeout
                )
        text = _result_text(result)
        if getattr(result, "is_error", False) or getattr(result, "isError", False):
            raise MCPError(text or f"Инструмент «{name}» вернул ошибку")
        return text


def _result_text(result: Any) -> str:
    """Собрать текстовые блоки ответа в одну строку: сервер вправе прислать их несколько."""
    blocks = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            blocks.append(text)
    return "\n".join(blocks).strip()


def _require_sdk() -> None:
    """Проверить, что пакет `mcp` есть в текущем интерпретаторе.

    Импорт SDK ленивый, и без этой проверки его отсутствие приходило пользователю голым
    «ModuleNotFoundError: No module named 'mcp'» в строке отказа подключения — то есть читалось
    как сбой сервера. На деле это всегда про окружение: пакет требует Python 3.10+, поэтому
    запуск приложения системным интерпретатором 3.9 не может его найти в принципе.
    """
    try:
        import mcp  # noqa: F401
    except ImportError as error:
        raise MCPError(
            "пакет mcp не установлен в текущем интерпретаторе "
            f"({sys.executable}, Python {sys.version_info.major}.{sys.version_info.minor}). "
            "Нужен Python 3.10+ и `pip install -r requirements.txt` в нём"
        ) from error


def _describe(error: BaseException) -> str:
    """Короткий текст ошибки для отчёта.

    ExceptionGroup от SDK сам по себе говорит только «unhandled errors in a TaskGroup», поэтому
    разворачиваем его до первой содержательной причины.
    """
    nested = getattr(error, "exceptions", None)
    if nested:
        return _describe(nested[0])
    text = str(error).strip()
    if not text:
        return type(error).__name__
    return f"{type(error).__name__}: {text}"
