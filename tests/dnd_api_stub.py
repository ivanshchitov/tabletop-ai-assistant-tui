"""Локальная заглушка внешнего API правил D&D 5e для прогонов.

Собственный MCP-сервер проекта ходит по HTTP во внешний API. Без заглушки его проверки
стучались бы в публичный dnd5eapi.co — то есть зависели бы от сети и от чужой доступности,
как прогон без OPENCODE_API_URL зависел бы от настоящей модели. Заглушка отвечает тем же
набором форм (индекс разделов, список раздела с поиском, запись раздела) и записывает
запрошенные пути, поэтому тест видит не только ответ, но и сам запрос.

Данные намеренно крошечные: они проверяют разбор и маршрутизацию, а не полноту справочника.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

SECTIONS = {
    "monsters": "/api/{ruleset}/monsters",
    "spells": "/api/{ruleset}/spells",
    "classes": "/api/{ruleset}/classes",
}

ENTRIES: Dict[str, Dict[str, Dict[str, Any]]] = {
    "monsters": {
        "goblin": {
            "index": "goblin",
            "name": "Goblin",
            "size": "Small",
            "armor_class": [{"type": "armor", "value": 15}],
            "hit_points": 7,
            "challenge_rating": 0.25,
            "actions": [{"name": "Scimitar", "desc": "Melee weapon attack."}],
        },
        "hobgoblin": {
            "index": "hobgoblin",
            "name": "Hobgoblin",
            "hit_points": 11,
            "challenge_rating": 0.5,
        },
    },
    "spells": {
        "fireball": {
            "index": "fireball",
            "name": "Fireball",
            "level": 3,
            "school": {"name": "Evocation"},
            "desc": ["Взрыв пламени в радиусе 20 футов."],
        },
        # Второе заклинание огня нужно цепочке дня 19: сводка по одной записи не отличила бы
        # «строка на запись» от «одна строка на всё».
        "fire-bolt": {
            "index": "fire-bolt",
            "name": "Fire Bolt",
            "level": 0,
            "school": {"name": "Evocation"},
            "range": "120 feet",
            "desc": ["Огненный снаряд в существо в пределах дальности. Урон 1d10 огнём."],
        },
    },
    "classes": {
        "wizard": {"index": "wizard", "name": "Wizard", "hit_die": 6},
    },
}

# Вторая ветка правил отличается ровно настолько, чтобы тест видел, какая из них запрошена.
RULESET_SUFFIX = {"2014": "", "2024": " (2024)"}


class _Handler(BaseHTTPRequestHandler):
    stub: "DndAPIStub" = None  # подставляется при запуске

    def do_GET(self) -> None:  # noqa: N802 - имя задано BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        self.stub.record(self.path)
        if self.stub.failure is not None:
            self._send(self.stub.failure, {"error": "заглушка отвечает отказом"})
            return

        parts = [part for part in parsed.path.split("/") if part]
        # Все пути начинаются с /api/<ветка правил>/...
        if len(parts) < 2 or parts[0] != "api" or parts[1] not in RULESET_SUFFIX:
            self._send(404, {"error": "Not Found"})
            return
        ruleset = parts[1]
        rest = parts[2:]

        if not rest:
            self._send(200, {name: path.format(ruleset=ruleset) for name, path in SECTIONS.items()})
            return

        section = rest[0]
        if section not in ENTRIES:
            self._send(404, {"error": "Not Found"})
            return

        if len(rest) == 1:
            query = parse_qs(parsed.query).get("name", [""])[0].lower()
            results = [
                {
                    "index": index,
                    "name": entry["name"] + RULESET_SUFFIX[ruleset],
                    "url": f"/api/{ruleset}/{section}/{index}",
                }
                for index, entry in ENTRIES[section].items()
                if query in index
            ]
            self._send(200, {"count": len(results), "results": results})
            return

        entry = ENTRIES[section].get(rest[1])
        if entry is None:
            self._send(404, {"error": "Not Found"})
            return
        body = dict(entry)
        body["name"] = body["name"] + RULESET_SUFFIX[ruleset]
        self._send(200, body)

    def _send(self, status: int, body: Dict[str, Any]) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # тишина в выводе прогона
        pass


class DndAPIStub:
    """HTTP-заглушка внешнего API: адрес отдаётся серверу переменной окружения."""

    def __init__(self) -> None:
        self.paths: List[str] = []
        self.failure: Optional[int] = None
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def record(self, path: str) -> None:
        with self._lock:
            self.paths.append(path)

    def start(self) -> "DndAPIStub":
        handler = type("_BoundHandler", (_Handler,), {"stub": self})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        """Базовый адрес в том же виде, в каком его принимает сервер: с суффиксом /api."""
        return f"http://127.0.0.1:{self.port}/api"
