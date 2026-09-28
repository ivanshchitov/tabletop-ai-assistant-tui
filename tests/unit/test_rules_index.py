import sqlite3
from pathlib import Path

import pytest

from core import rules_index


def test_fixed_chunking_overlaps_and_keeps_all_text():
    text = "0123456789" * 5

    chunks = rules_index.fixed_chunks(text, chunk_size=20, overlap=5)

    assert len(chunks) == 3
    assert chunks[0].text == text[:20]
    assert chunks[1].text.startswith(chunks[0].text[-5:])
    assert "".join([chunks[0].text, chunks[1].text[5:], chunks[2].text[5:]]) == text


def test_structural_chunking_keeps_section_metadata_and_content():
    pages = [
        rules_index.PageText(1, "Setup\nPlace the board.\nTake cards."),
        rules_index.PageText(2, "Turn\nDraw cards.\nClaim a route."),
    ]

    chunks = rules_index.structural_chunks("rules.pdf", "Rules", pages, chunk_size=100)

    assert [chunk.section for chunk in chunks] == ["Setup", "Turn"]
    assert "Place the board." in chunks[0].text
    assert "Claim a route." in chunks[1].text
    assert all(chunk.source == "rules.pdf" for chunk in chunks)
    assert [chunk.chunk_id for chunk in chunks] == ["rules.pdf:structural:1", "rules.pdf:structural:2"]


def test_structural_chunking_splits_a_section_at_the_limit():
    pages = [rules_index.PageText(1, "Rules\n" + "word " * 40)]

    chunks = rules_index.structural_chunks("r.pdf", "R", pages, chunk_size=40)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 40 for chunk in chunks)
    assert all(chunk.section == "Rules" for chunk in chunks)


def test_hash_embeddings_are_normalized_and_repeatable():
    first = rules_index.embed("Draw a card and build a road")

    assert first == rules_index.embed("Draw a card and build a road")
    assert len(first) == rules_index.EMBEDDING_DIMENSIONS
    assert sum(value * value for value in first) == pytest.approx(1.0)


def test_russian_rules_query_expands_common_terms():
    from_query = rules_index.embed("Как строить дороги в CATAN?")
    from_text = rules_index.embed("Build a road in Catan")

    assert rules_index.cosine(from_query, from_text) > 0.1


def test_build_and_search_persist_both_strategies(tmp_path, monkeypatch):
    document = tmp_path / "rules.pdf"
    document.write_bytes(b"fixture")
    database = tmp_path / "index.sqlite3"

    monkeypatch.setattr(
        rules_index,
        "read_pdf",
        lambda _path: ("Board Game Rules", [
            rules_index.PageText(1, "Setup\nPlace the game board and give each player five cards."),
            rules_index.PageText(2, "Turn\nDraw a card or build a road with matching cards."),
        ]),
    )

    report = rules_index.build_index(tmp_path, database)

    assert report.files == 1
    assert report.pages == 2
    assert report.chunks["fixed"] > 0
    assert report.chunks["structural"] == 2
    assert rules_index.index_exists(database)
    fixed = rules_index.search(database, "build a road", strategy="fixed")
    structured = rules_index.search(database, "build a road", strategy="structural")
    assert "build a road" in fixed[0].text
    assert structured[0].section == "Turn"
    assert structured[0].score > 0
    stats = rules_index.strategy_stats(database)
    assert stats["fixed"]["chunks"] == report.chunks["fixed"]
    assert stats["structural"]["average_chars"] > 0


def test_failed_build_does_not_leave_a_partial_index(tmp_path, monkeypatch):
    document = tmp_path / "broken.pdf"
    document.write_bytes(b"fixture")
    database = tmp_path / "index.sqlite3"
    monkeypatch.setattr(rules_index, "read_pdf", lambda _path: (_ for _ in ()).throw(ValueError("bad PDF")))

    with pytest.raises(rules_index.RulesIndexError, match="bad PDF"):
        rules_index.build_index(tmp_path, database)

    assert not rules_index.index_exists(database)


def test_empty_corpus_is_rejected(tmp_path):
    with pytest.raises(rules_index.RulesIndexError, match="PDF"):
        rules_index.build_index(tmp_path, tmp_path / "index.sqlite3")


def test_page_text_splits_heading_from_body():
    page = rules_index.PageText(4, "CHAPTER 2: Playing\nThe player draws a card.")

    assert rules_index.extract_sections(page) == [
        ("CHAPTER 2: Playing", "The player draws a card.")
    ]


def test_structural_parser_does_not_treat_body_words_as_headings():
    page = rules_index.PageText(1, "Turn\nEach player takes a turn.\nHe draws one card.")

    assert rules_index.extract_sections(page) == [
        ("Turn", "Each player takes a turn. He draws one card.")
    ]


def test_index_has_metadata_columns(tmp_path, monkeypatch):
    document = tmp_path / "rules.pdf"
    document.write_bytes(b"fixture")
    database = tmp_path / "index.sqlite3"
    monkeypatch.setattr(rules_index, "read_pdf", lambda _path: ("Rules", [rules_index.PageText(1, "Rules\nDraw cards.")]))

    rules_index.build_index(tmp_path, database)
    with sqlite3.connect(str(database)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}

    assert {"source", "title", "section", "chunk_id", "embedding"} <= columns
