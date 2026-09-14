"""Долговременная память агента: отдельный файл, категории записей, немедленная запись."""

import json
from pathlib import Path

import pytest

from core import config, memory_layers
from core.long_term_memory import LongTermMemory


@pytest.fixture
def memory_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.json"


def test_missing_file_gives_empty_memory(memory_path):
    memory = LongTermMemory(path=memory_path)

    assert memory.records() == ()
    assert memory.get("опыт") is None


def test_corrupted_json_is_tolerated(memory_path):
    memory_path.write_text("{не json", encoding="utf-8")

    assert LongTermMemory(path=memory_path).records() == ()


def test_remember_persists_immediately(memory_path):
    memory = LongTermMemory(path=memory_path)
    memory.remember(key="опыт", value="более 300 партий", category=memory_layers.CATEGORY_PROFILE)

    saved = json.loads(memory_path.read_text(encoding="utf-8"))
    assert saved["entries"]["опыт"] == {
        "value": "более 300 партий",
        "category": memory_layers.CATEGORY_PROFILE,
    }


def test_records_survive_reload(memory_path):
    LongTermMemory(path=memory_path).remember(
        key="решения", value="без таймера", category=memory_layers.CATEGORY_DECISION
    )
    reloaded = LongTermMemory(path=memory_path)

    records = reloaded.records()
    assert [record.key for record in records] == ["решения"]
    assert records[0].layer == memory_layers.LONG_TERM
    assert records[0].category == memory_layers.CATEGORY_DECISION
    assert records[0].value == "без таймера"


def test_repeat_keeps_one_entry_with_the_last_value(memory_path):
    memory = LongTermMemory(path=memory_path)
    memory.remember(key="опыт", value="новичок", category=memory_layers.CATEGORY_PROFILE)
    memory.remember(key="опыт", value="опытный игрок", category=memory_layers.CATEGORY_PROFILE)

    assert memory.get("опыт") == "опытный игрок"
    assert len(memory.records()) == 1


def test_forget_removes_one_entry(memory_path):
    memory = LongTermMemory(path=memory_path)
    memory.remember(key="опыт", value="опытный", category=memory_layers.CATEGORY_PROFILE)
    memory.remember(key="решения", value="без таймера", category=memory_layers.CATEGORY_DECISION)

    assert memory.forget("опыт") is True
    assert memory.forget("нет такого") is False
    assert [record.key for record in LongTermMemory(path=memory_path).records()] == ["решения"]


def test_clear_wipes_the_file(memory_path):
    memory = LongTermMemory(path=memory_path)
    memory.remember(key="опыт", value="опытный", category=memory_layers.CATEGORY_PROFILE)
    memory.clear()

    assert memory.records() == ()
    assert LongTermMemory(path=memory_path).records() == ()


def test_entries_without_category_read_as_notes(memory_path):
    memory_path.write_text(
        json.dumps({"entries": {"заметка 1": {"value": "филлер про пингвинов"}}}),
        encoding="utf-8",
    )

    records = LongTermMemory(path=memory_path).records()
    assert records[0].category == memory_layers.CATEGORY_NOTE


def test_save_survives_unwritable_path(tmp_path):
    """Ошибка записи не должна ронять приложение — память просто не сохранится."""
    target = tmp_path / "memory.json"
    target.mkdir()
    memory = LongTermMemory(path=target)

    memory.remember(key="опыт", value="опытный", category=memory_layers.CATEGORY_PROFILE)

    assert memory.get("опыт") == "опытный"


def test_memory_file_defaults_into_repository_root():
    """Путь выводится из расположения кода: иначе прогон писал бы в реальный файл репозитория."""
    assert config.MEMORY_FILE == config.BASE_DIR / "memory.json"


def test_memory_file_override_from_environment(monkeypatch, tmp_path):
    import importlib

    monkeypatch.setenv("TABLETOP_MEMORY_FILE", str(tmp_path / "other-memory.json"))
    try:
        assert importlib.reload(config).MEMORY_FILE == tmp_path / "other-memory.json"
    finally:
        monkeypatch.delenv("TABLETOP_MEMORY_FILE", raising=False)
        importlib.reload(config)


def test_memory_value_limit_is_small():
    """Запись уходит в каждый запрос, поэтому её длина ограничена."""
    assert 20 <= config.MEMORY_VALUE_MAX_CHARS <= 500
