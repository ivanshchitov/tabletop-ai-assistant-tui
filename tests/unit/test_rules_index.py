import sqlite3
from pathlib import Path

import pytest

from core import rules_index


class SemanticEmbeddings:
    model = "semantic-test"

    def __init__(self):
        self.documents = []
        self.queries = []

    def embed(self, text):
        self.documents.append(text)
        return [1.0, 0.0] if "brick" in text else [0.0, 1.0]

    def embed_query(self, query):
        self.queries.append(query)
        return [1.0, 0.0]


@pytest.fixture
def old_hash_index(tmp_path, monkeypatch):
    (tmp_path / "rules.pdf").write_bytes(b"fixture")
    monkeypatch.setattr(rules_index, "read_pdf", lambda _path: ("Rules", [
        rules_index.PageText(1, "Building\nA road costs one brick and one lumber."),
        rules_index.PageText(2, "Turn\nDraw a card."),
    ]))
    database = tmp_path / "index.sqlite3"
    rules_index.build_index(tmp_path, database)
    return database


def test_local_search_migrates_old_index_and_reuses_vectors(old_hash_index):
    provider = SemanticEmbeddings()
    original = rules_index.search(old_hash_index, "Draw a card", minimum_score=-1)
    results = rules_index.search(old_hash_index, "construction materials", embeddings=provider)
    assert results[0].section == "Building"
    assert results[0].score == pytest.approx(1)
    assert len(provider.documents) == 3  # Both strategies, once.
    rules_index.search(old_hash_index, "another question", embeddings=provider)
    assert len(provider.documents) == 3
    assert provider.queries == ["construction materials", "another question"]
    assert rules_index.search(old_hash_index, "Draw a card", minimum_score=-1) == original
    provider.model = "another-model"
    rules_index.compare(old_hash_index, "materials", embeddings=provider)
    assert len(provider.documents) == 6


def test_failed_local_vector_preparation_is_atomic(old_hash_index):
    class Broken(SemanticEmbeddings):
        def embed(self, text):
            if self.documents:
                raise rules_index.RulesIndexError("offline")
            return super().embed(text)

    with pytest.raises(rules_index.RulesIndexError, match="offline"):
        rules_index.search(old_hash_index, "cost", embeddings=Broken())
    provider = SemanticEmbeddings()
    rules_index.search(old_hash_index, "cost", embeddings=provider)
    assert len(provider.documents) == 3


def test_local_rebuild_failure_keeps_previous_index(old_hash_index):
    previous = old_hash_index.read_bytes()
    class Broken(SemanticEmbeddings):
        def embed(self, text):
            raise rules_index.RulesIndexError("offline")

    with pytest.raises(rules_index.RulesIndexError, match="offline"):
        rules_index.build_index(old_hash_index.parent, old_hash_index, embeddings=Broken())
    assert old_hash_index.read_bytes() == previous


def test_local_rebuild_prepares_vectors_before_search(old_hash_index):
    provider = SemanticEmbeddings()
    rules_index.build_index(old_hash_index.parent, old_hash_index, embeddings=provider)
    assert len(provider.documents) == 3
    assert rules_index.search(old_hash_index, "materials", embeddings=provider)[0].section == "Building"
    assert len(provider.documents) == 3


def test_local_search_rejects_query_dimension_mismatch(old_hash_index):
    provider = SemanticEmbeddings()
    provider.embed_query = lambda query: [1, 0, 0]
    with pytest.raises(rules_index.RulesIndexError, match="размерност"):
        rules_index.search(old_hash_index, "cost", embeddings=provider)


def test_local_search_rejects_document_dimension_mismatch(old_hash_index):
    class Uneven(SemanticEmbeddings):
        def embed(self, text):
            return super().embed(text) if not self.documents else [1, 0, 0]

    with pytest.raises(rules_index.RulesIndexError, match="размерност"):
        rules_index.search(old_hash_index, "cost", embeddings=Uneven())


def test_missing_index_makes_no_embedding_requests(tmp_path):
    provider = SemanticEmbeddings()
    assert rules_index.search(tmp_path / "missing", "cost", embeddings=provider) == []
    assert not provider.documents and not provider.queries


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


def test_context_message_names_file_section_and_chunk_id():
    result = rules_index.SearchResult(
        "catan.pdf", "CATAN", "Building", "catan.pdf:structural:2",
        "A road costs one brick and one lumber.", 0.8, "structural",
    )

    message = rules_index.context_message([result])

    assert "[catan.pdf | CATAN; Building; catan.pdf:structural:2]" in message
    assert "A road costs one brick and one lumber." in message


def test_context_message_of_empty_selection_is_none():
    assert rules_index.context_message([]) is None


def test_index_has_metadata_columns(tmp_path, monkeypatch):
    document = tmp_path / "rules.pdf"
    document.write_bytes(b"fixture")
    database = tmp_path / "index.sqlite3"
    monkeypatch.setattr(rules_index, "read_pdf", lambda _path: ("Rules", [rules_index.PageText(1, "Rules\nDraw cards.")]))

    rules_index.build_index(tmp_path, database)
    with sqlite3.connect(str(database)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}

    assert {"source", "title", "section", "chunk_id", "embedding"} <= columns
