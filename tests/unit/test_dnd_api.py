"""Обращения собственного MCP-сервера к внешнему API — против локальной заглушки.

Настоящий dnd5eapi.co здесь не вызывается: заглушка поднимается на localhost и записывает
запрошенные пути, поэтому прогон не зависит от сети, а проверка видит, что именно ушло.
"""

import json
import urllib.request

import pytest

from tests.dnd_api_stub import DndAPIStub


@pytest.fixture
def stub():
    server = DndAPIStub()
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def test_stub_serves_section_index(stub):
    status, body = _get(f"{stub.url}/2014")
    assert status == 200
    assert body["monsters"] == "/api/2014/monsters"


def test_stub_serves_section_listing_with_search(stub):
    status, body = _get(f"{stub.url}/2014/monsters?name=gob")
    assert status == 200
    assert [item["index"] for item in body["results"]] == ["goblin", "hobgoblin"]


def test_stub_serves_entry_by_index(stub):
    status, body = _get(f"{stub.url}/2014/monsters/goblin")
    assert status == 200
    assert body["name"] == "Goblin"
    assert body["hit_points"] == 7


def test_stub_serves_second_ruleset(stub):
    status, body = _get(f"{stub.url}/2024/monsters/goblin")
    assert status == 200
    assert body["name"] == "Goblin (2024)"


def test_stub_answers_404_for_unknown_entry(stub):
    with pytest.raises(Exception) as excinfo:
        _get(f"{stub.url}/2014/monsters/no-such-entry")
    assert "404" in str(excinfo.value)


def test_stub_records_requested_paths(stub):
    _get(f"{stub.url}/2014/monsters/goblin")
    assert stub.paths == ["/api/2014/monsters/goblin"]


# --- модуль обращения к внешнему API ---


@pytest.fixture
def api(stub, monkeypatch):
    monkeypatch.setenv("TABLETOP_DND_API_URL", stub.url)
    from mcp_server.dnd_api import DndAPI

    return DndAPI()


def test_api_reads_base_url_from_environment(api, stub):
    api.sections()
    assert stub.paths == ["/api/2014"]


def test_sections_come_from_api_index(api):
    assert set(api.sections()) >= {"monsters", "spells", "classes"}


def test_entry_returns_record_fields(api):
    entry = api.entry("monsters", "goblin")
    assert entry["name"] == "Goblin"
    assert entry["hit_points"] == 7


def test_search_returns_matches(api):
    results = api.search("monsters", "gob", limit=5)
    assert [item["index"] for item in results] == ["goblin", "hobgoblin"]


def test_search_respects_limit(api):
    assert len(api.search("monsters", "gob", limit=1)) == 1


def test_default_ruleset_is_2014(api, stub):
    api.entry("monsters", "goblin")
    assert stub.paths == ["/api/2014/monsters/goblin"]


def test_explicit_ruleset_changes_path(api, stub):
    entry = api.entry("monsters", "goblin", ruleset="2024")
    assert stub.paths == ["/api/2024/monsters/goblin"]
    assert entry["name"] == "Goblin (2024)"


def test_unknown_entry_raises_api_error(api):
    from mcp_server.dnd_api import DndAPIError

    with pytest.raises(DndAPIError) as excinfo:
        api.entry("monsters", "no-such-entry")
    assert "no-such-entry" in str(excinfo.value)


def test_unavailable_api_raises_api_error(stub, monkeypatch):
    """Сервер внешнего API не отвечает — это ошибка данных, а не падение процесса."""
    monkeypatch.setenv("TABLETOP_DND_API_URL", f"http://127.0.0.1:{stub.port + 1}/api")
    from mcp_server.dnd_api import DndAPI, DndAPIError

    with pytest.raises(DndAPIError):
        DndAPI(timeout=1).sections()


def test_api_failure_status_raises_api_error(api, stub):
    stub.failure = 500
    from mcp_server.dnd_api import DndAPIError

    with pytest.raises(DndAPIError):
        api.sections()
