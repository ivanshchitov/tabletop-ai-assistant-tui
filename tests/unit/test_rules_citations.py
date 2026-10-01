"""Проверка цитат RAG-ответа: источники и цитаты обязаны происходить из переданных чанков."""

from core import config, rules_citations
from core.rules_index import SearchResult


def source(
    text: str = "A road costs one brick and one lumber.",
    *,
    title: str = "CATAN",
    section: str = "Building",
    chunk_id: str = "catan.pdf:structural:2",
    file: str = "catan.pdf",
) -> SearchResult:
    return SearchResult(file, title, section, chunk_id, text, 0.8, "structural")


def test_min_chars_and_retries_are_fixed_constants():
    assert config.CITATION_MIN_CHARS == 20
    assert config.CITATION_RETRIES == 1


def test_exact_citation_and_named_source_are_confirmed():
    chunk = source()
    answer = (
        "Дорога стоит одну кирпичную и одну деревянную карту.\n\n"
        "Цитаты: «A road costs one brick and one lumber.»\n"
        "Источники: catan.pdf, раздел Building (catan.pdf:structural:2)."
    )
    check = rules_citations.check_answer(answer, (chunk,))
    assert check.confirmed
    assert check.violations == ()


def test_case_and_whitespace_are_normalized():
    chunk = source("A road costs\n   one brick and one LUMBER.")
    answer = "Цитата: «a road costs one brick and one lumber». Источник: catan.pdf:structural:2."
    assert rules_citations.check_answer(answer, (chunk,)).confirmed


def test_short_quote_does_not_confirm():
    chunk = source()
    answer = "Дорога стоит кирпич. Цитата: «one brick». Источник: catan.pdf:structural:2."
    check = rules_citations.check_answer(answer, (chunk,))
    assert not check.confirmed
    assert rules_citations.NO_QUOTE_MARKER in check.violations


def test_quote_from_undelivered_chunk_does_not_confirm():
    chunk = source()
    answer = (
        "Ответ. Цитата: «Traveling on roads depends on difficult terrain».\n"
        "Источник: catan.pdf:structural:2."
    )
    check = rules_citations.check_answer(answer, (chunk,))
    assert not check.confirmed
    assert rules_citations.NO_QUOTE_MARKER in check.violations


def test_answer_without_any_source_identifier_is_a_violation():
    chunk = source()
    answer = "Дорога стоит кирпич и дерево. Цитата: «A road costs one brick and one lumber»."
    check = rules_citations.check_answer(answer, (chunk,))
    assert not check.confirmed
    assert rules_citations.NO_SOURCE_MARKER in check.violations


def test_title_only_is_not_enough_identifier():
    chunk = source()
    answer = "Про CATAN. Цитата: «A road costs one brick and one lumber»."
    check = rules_citations.check_answer(answer, (chunk,))
    assert not check.confirmed
    assert rules_citations.NO_SOURCE_MARKER in check.violations


def test_empty_answer_and_empty_sources_are_violations_not_crashes():
    chunk = source()
    check = rules_citations.check_answer("", (chunk,))
    assert not check.confirmed
    assert rules_citations.NO_SOURCE_MARKER in check.violations
    assert rules_citations.NO_QUOTE_MARKER in check.violations

    check = rules_citations.check_answer("Ответ. Цитата: «A road costs one brick and one lumber».", ())
    assert not check.confirmed


def test_any_delivered_source_counts_and_shortest_quote_wins():
    catan = source()
    ticket = source(
        "A face-up locomotive is your only card for that turn.",
        title="Ticket to Ride",
        section="Locomotives",
        chunk_id="ticket.pdf:structural:1",
        file="ticket.pdf",
    )
    answer = (
        "Ответ по Ticket to Ride. "
        "Цитата: «A face-up locomotive is your only card for that turn». "
        "Источник: ticket.pdf."
    )
    check = rules_citations.check_answer(answer, (catan, ticket))
    assert check.confirmed


def test_retry_prompt_lists_violations_and_disclaimer_text_is_fixed():
    violations = (rules_citations.NO_QUOTE_MARKER,)
    prompt = rules_citations.retry_prompt(violations)
    assert rules_citations.NO_QUOTE_MARKER in prompt
    text = rules_citations.disclaimer_text()
    assert text
    assert "не знаю" in text.lower()
