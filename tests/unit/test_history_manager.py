"""Загрузка, накопление и сохранение истории диалогов."""

import json

import pytest

from core.history_manager import HistoryManager


def test_missing_file_gives_empty_history(tmp_path):
    manager = HistoryManager(path=tmp_path / "nope.json")
    assert manager.dialogues == []
    assert manager.count() == 0


def test_corrupted_json_is_tolerated(history_path):
    """Битый файл истории не должен ронять запуск приложения."""
    history_path.write_text("{не json", encoding="utf-8")
    assert HistoryManager(path=history_path).dialogues == []


def test_non_list_payload_is_ignored(history_path):
    history_path.write_text(json.dumps({"question": "q"}), encoding="utf-8")
    assert HistoryManager(path=history_path).dialogues == []


def test_add_appends_and_persists_immediately(history_path):
    manager = HistoryManager(path=history_path)
    manager.add("Правила Splendor?", "Собирайте фишки.")

    saved = json.loads(history_path.read_text(encoding="utf-8"))
    assert saved == [{"question": "Правила Splendor?", "answer": "Собирайте фишки."}]


def test_history_survives_reload(history_path):
    HistoryManager(path=history_path).add("Вопрос", "Ответ")
    reloaded = HistoryManager(path=history_path)
    assert reloaded.dialogues == [{"question": "Вопрос", "answer": "Ответ"}]


def test_add_trims_to_limit_keeping_latest(history_path):
    manager = HistoryManager(path=history_path, limit=3)
    for i in range(5):
        manager.add(f"q{i}", f"a{i}")

    assert [d["question"] for d in manager.dialogues] == ["q2", "q3", "q4"]
    assert manager.count() == 3


def test_load_trims_oversized_file_to_limit(history_path):
    payload = [{"question": f"q{i}", "answer": f"a{i}"} for i in range(10)]
    history_path.write_text(json.dumps(payload), encoding="utf-8")

    manager = HistoryManager(path=history_path, limit=4)
    assert [d["question"] for d in manager.dialogues] == ["q6", "q7", "q8", "q9"]


def test_clear_empties_memory_and_file(history_path):
    manager = HistoryManager(path=history_path)
    manager.add("q", "a")
    manager.clear()

    assert manager.dialogues == []
    assert json.loads(history_path.read_text(encoding="utf-8")) == []


def test_cyrillic_is_stored_readable(history_path):
    HistoryManager(path=history_path).add("Каркассон", "Мипл 🎲")
    raw = history_path.read_text(encoding="utf-8")
    assert "Каркассон" in raw
    assert "\\u" not in raw


def test_save_survives_unwritable_path(tmp_path):
    """Ошибка записи не должна ронять приложение — история просто не сохранится."""
    directory = tmp_path / "history.json"
    directory.mkdir()  # запись по этому пути заведомо провалится
    manager = HistoryManager(path=directory)
    manager.add("q", "a")  # не должно выбросить исключение
    assert manager.dialogues == [{"question": "q", "answer": "a"}]


def test_default_limit_comes_from_config():
    from core import config

    manager = HistoryManager(path=config.BASE_DIR / "does-not-exist.json")
    assert manager.limit == config.HISTORY_LIMIT


@pytest.mark.parametrize("limit", [1, 2, 50])
def test_limit_is_respected_for_various_sizes(history_path, limit):
    manager = HistoryManager(path=history_path, limit=limit)
    for i in range(limit + 3):
        manager.add(f"q{i}", f"a{i}")
    assert manager.count() == limit
