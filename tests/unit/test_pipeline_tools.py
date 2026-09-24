"""Инструменты обработки и сохранения собственного сервера: сводка и запись в файл (день 19).

Без протокола и без сети: сводка — чистое преобразование текста, сохранение — запись во
временный каталог, подменённый так же, как в прогоне приложения.
"""

import json

import pytest

from mcp_server.pipeline_tools import PIPELINE_TOOLS, PipelineTools

SEARCH_RESULT = "Найдено в разделе «spells» (редакция 2014), записей: 2\n" + json.dumps(
    [
        {
            "index": "fireball",
            "name": "Fireball",
            "level": 3,
            "school": "Evocation",
            "desc": "Взрыв пламени в радиусе 20 футов. Существа делают спасбросок.",
        },
        {
            "index": "fire-bolt",
            "name": "Fire Bolt",
            "level": 0,
            "school": "Evocation",
            "range": "120 feet",
            "desc": "Огненный снаряд в существо. Урон 1d10 огнём.",
        },
    ],
    ensure_ascii=False,
    indent=2,
)


@pytest.fixture
def exports(tmp_path, monkeypatch):
    path = tmp_path / "exports"
    monkeypatch.setenv("TABLETOP_EXPORTS_DIR", str(path))
    return path


@pytest.fixture
def tools():
    return PipelineTools()


# --- объявление ---


def test_summary_and_save_tools_are_declared():
    assert {tool.name for tool in PIPELINE_TOOLS} == {"dnd_summarize", "save_to_file"}


def test_schemas_require_text():
    for tool in PIPELINE_TOOLS:
        assert "text" in tool.input_schema["required"]
        assert tool.input_schema["properties"]["text"]["type"] == "string"
    save = next(tool for tool in PIPELINE_TOOLS if tool.name == "save_to_file")
    assert "filename" in save.input_schema["required"]


# --- сводка ---


def test_summary_has_a_line_per_record(tools):
    ok, text = tools.call_result("dnd_summarize", {"text": SEARCH_RESULT})
    assert ok
    lines = [line for line in text.splitlines() if line.startswith("- ")]
    assert len(lines) == 2
    assert lines[0].startswith("- Fireball")
    assert "уровень 3" in lines[0]
    assert "Evocation" in lines[0]
    # Первое предложение описания, не всё описание.
    assert "Взрыв пламени в радиусе 20 футов." in lines[0]
    assert "спасбросок" not in lines[0]
    assert "Fire Bolt" in lines[1]
    assert "заговор" in lines[1]


def test_summary_header_names_the_count(tools):
    _, text = tools.call_result("dnd_summarize", {"text": SEARCH_RESULT})
    assert text.splitlines()[0].startswith("Сводка: 2 записи")


def test_summary_is_deterministic(tools):
    assert tools.call("dnd_summarize", {"text": SEARCH_RESULT}) == tools.call(
        "dnd_summarize", {"text": SEARCH_RESULT}
    )


def test_summary_respects_max_items(tools):
    ok, text = tools.call_result("dnd_summarize", {"text": SEARCH_RESULT, "max_items": 1})
    assert ok
    assert len([line for line in text.splitlines() if line.startswith("- ")]) == 1
    assert "ещё 1" in text


def test_summary_reads_plain_search_list(tools):
    """Поиск без подробностей — строки «- index: name»: сводка тоже понимает их."""
    plain = "Найдено в разделе «spells» (редакция 2014):\n- fireball: Fireball\n- fire-bolt: Fire Bolt"
    ok, text = tools.call_result("dnd_summarize", {"text": plain})
    assert ok
    assert "- Fireball" in text and "- Fire Bolt" in text


def test_summary_rejects_text_without_records(tools):
    ok, text = tools.call_result("dnd_summarize", {"text": "просто слова без записей"})
    assert not ok
    assert "записей" in text


def test_summary_requires_text(tools):
    ok, text = tools.call_result("dnd_summarize", {})
    assert not ok
    assert "text" in text


def test_summary_rejects_bad_max_items(tools):
    ok, text = tools.call_result("dnd_summarize", {"text": SEARCH_RESULT, "max_items": 0})
    assert not ok


# --- сохранение ---


def test_save_writes_markdown_file(tools, exports):
    ok, text = tools.call_result("save_to_file", {"text": "сводка", "filename": "fire-spells"})
    assert ok
    target = exports / "fire-spells.md"
    assert target.read_text(encoding="utf-8") == "сводка"
    assert str(target) in text
    assert "6 символов" in text


def test_save_keeps_md_extension_once(tools, exports):
    tools.call_result("save_to_file", {"text": "x", "filename": "notes.md"})
    assert (exports / "notes.md").exists()


def test_save_never_overwrites(tools, exports):
    tools.call_result("save_to_file", {"text": "первый", "filename": "fire-spells"})
    ok, text = tools.call_result("save_to_file", {"text": "второй", "filename": "fire-spells"})
    assert ok
    assert (exports / "fire-spells.md").read_text(encoding="utf-8") == "первый"
    assert (exports / "fire-spells-2.md").read_text(encoding="utf-8") == "второй"
    assert "fire-spells-2.md" in text


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd", "sub/dir", "..", "", "   "])
def test_save_rejects_names_leaving_the_directory(tools, exports, tmp_path, name):
    ok, _ = tools.call_result("save_to_file", {"text": "x", "filename": name})
    assert not ok
    assert not (tmp_path / "escape.md").exists()
    assert not exports.exists() or not any(exports.iterdir())


def test_save_rejects_empty_text(tools, exports):
    ok, text = tools.call_result("save_to_file", {"text": "  ", "filename": "empty"})
    assert not ok
    assert not (exports / "empty.md").exists()


def test_unknown_tool_is_a_failure(tools):
    ok, text = tools.call_result("drop_everything", {})
    assert not ok
    assert "не объявлен" in text
