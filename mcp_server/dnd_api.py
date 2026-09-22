"""Обращения к внешнему API правил D&D 5e (dnd5eapi.co).

Модуль живёт вне пакетов `core/` и `ui/`: он часть отдельного процесса — собственного
MCP-сервера проекта. Приложение знает о сервере только команду запуска из реестра описаний,
поэтому ни адрес этого API, ни имена его разделов в коде приложения не появляются.

Базовый адрес читается из окружения (`TABLETOP_DND_API_URL`) при каждом обращении: иначе
автоматический прогон стучался бы в публичный сервис — ровно та же причина, по которой адрес
модели задаётся `OPENCODE_API_URL`.

Любой сбой (сеть, таймаут, неизвестная запись, отказ сервиса) превращается в `DndAPIError`:
серверный процесс от него не падает, а текст ошибки уходит в результат вызова инструмента —
его читает модель, которая и выбирала аргументы.
"""

import os
from typing import Any, Dict, List, Optional

import requests

DEFAULT_API_URL = "https://www.dnd5eapi.co/api"
# Две ветки правил, которые публикует сам сервис. Значение по умолчанию — редакция 2014 года:
# её данные заполнены полностью, тогда как ветка 2024 года пока покрывает не все разделы.
RULESETS = ("2014", "2024")
DEFAULT_RULESET = RULESETS[0]
DEFAULT_TIMEOUT = 20.0


class DndAPIError(Exception):
    """Внешний API не ответил или ответил отказом."""


def base_url() -> str:
    return os.getenv("TABLETOP_DND_API_URL", DEFAULT_API_URL).rstrip("/")


class DndAPI:
    """Три обращения, которых хватает на весь справочник: индекс, список раздела, запись."""

    def __init__(self, url: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._url = url
        self.timeout = timeout

    @property
    def url(self) -> str:
        return (self._url or base_url()).rstrip("/")

    def sections(self, ruleset: str = DEFAULT_RULESET) -> List[str]:
        """Разделы справочника так, как их перечисляет сам сервис.

        Список не зашит в код: новый раздел на стороне сервиса становится доступен
        инструментам без единой правки здесь.
        """
        index = self._get(f"/{ruleset}")
        return sorted(str(name) for name in index)

    def search(
        self,
        section: str,
        query: str = "",
        limit: int = 5,
        ruleset: str = DEFAULT_RULESET,
    ) -> List[Dict[str, Any]]:
        """Записи раздела, чьё имя похоже на запрос, не больше `limit` штук."""
        path = f"/{ruleset}/{section}"
        params = {"name": query} if query else None
        body = self._get(path, params=params)
        results = body.get("results", []) if isinstance(body, dict) else []
        return list(results)[: max(0, limit)]

    def entry(self, section: str, index: str, ruleset: str = DEFAULT_RULESET) -> Dict[str, Any]:
        """Полная запись раздела по её идентификатору."""
        return self._get(f"/{ruleset}/{section}/{index}")

    def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Any:
        address = f"{self.url}{path}"
        try:
            response = requests.get(address, params=params, timeout=self.timeout)
        except requests.exceptions.RequestException as error:
            raise DndAPIError(f"внешний API недоступен ({address}): {error}") from error
        if response.status_code == 404:
            raise DndAPIError(f"внешний API не знает записи по пути {path}")
        if response.status_code >= 400:
            raise DndAPIError(f"внешний API ответил кодом {response.status_code} на {path}")
        try:
            return response.json()
        except ValueError as error:
            raise DndAPIError(f"внешний API вернул не JSON на {path}") from error
