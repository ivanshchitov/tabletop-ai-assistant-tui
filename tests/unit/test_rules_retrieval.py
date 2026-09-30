import json
from dataclasses import FrozenInstanceError

import pytest

from core.rules_index import SearchResult


def candidates():
    return (
        SearchResult("dnd.pdf", "D&D", "Travel", "dnd:1", "Travel rules.", 0.9, "structural"),
        SearchResult("catan.pdf", "CATAN", "Build", "catan:2", "One brick and one lumber.", 0.5, "structural"),
        SearchResult("catan.pdf", "CATAN", "Roads", "catan:3", "Road building costs.", 0.4, "structural"),
        SearchResult("catan.pdf", "CATAN", "Costs", "catan:4", "Pay the road cost.", 0.3, "structural"),
    )


def test_second_stage_reorders_sources_and_explains_both_exclusion_paths():
    from core import rules_retrieval as retrieval

    ratings = retrieval.parse_ratings(json.dumps({"results": [
        {"id": 4, "score": 0.7, "reason": "Supporting context"},
        {"id": 1, "score": 0.1, "reason": "Different game"},
        {"id": 3, "score": 0.95, "reason": "Exact rule"},
        {"id": 2, "score": 0.8, "reason": "Road cost"},
    ]}), 4)
    selection = retrieval.select_candidates(
        candidates(), ratings, retrieval.RetrievalSettings(before=4, after=2, threshold=0.6)
    )

    assert [result.chunk_id for result in selection.results] == ["catan:3", "catan:2"]
    decisions = {decision.result.chunk_id: decision for decision in selection.decisions}
    assert decisions["dnd:1"].disposition == "below_threshold"
    assert decisions["catan:4"].disposition == "top_k"
    assert decisions["catan:3"].disposition == "selected"
    assert decisions["dnd:1"].relevance.reason == "Different game"
    assert decisions["catan:3"].result.score == 0.4
    assert decisions["catan:3"].relevance.score == 0.95


def test_threshold_is_inclusive_and_ties_keep_candidate_order_not_model_order():
    from core import rules_retrieval as retrieval

    ratings = retrieval.parse_ratings(json.dumps({"results": [
        {"id": 3, "score": 0.6, "reason": "Boundary"},
        {"id": 2, "score": 0.6, "reason": "Boundary"},
        {"id": 1, "score": 0.599, "reason": "Below"},
        {"id": 4, "score": 0.6, "reason": "Boundary"},
    ]}), 4)
    selection = retrieval.select_candidates(
        candidates(), ratings, retrieval.RetrievalSettings(before=4, after=2, threshold=0.6)
    )

    assert [result.chunk_id for result in selection.results] == ["catan:2", "catan:3"]
    assert selection.decisions[0].disposition == "below_threshold"
    assert selection.decisions[3].disposition == "top_k"


def test_all_candidates_below_threshold_produce_no_sources():
    from core import rules_retrieval as retrieval

    ratings = retrieval.parse_ratings(
        '{"results": [{"id": 1, "score": 0.2, "reason": "Wrong game"}]}', 1
    )
    selection = retrieval.select_candidates(
        candidates()[:1], ratings, retrieval.RetrievalSettings(threshold=0.6)
    )

    assert selection.results == ()
    assert selection.decisions[0].disposition == "below_threshold"


def test_no_candidates_produce_no_sources():
    from core import rules_retrieval as retrieval

    selection = retrieval.select_candidates((), (), retrieval.RetrievalSettings())

    assert selection.results == ()
    assert selection.decisions == ()


def test_first_stage_budget_cannot_be_bypassed_by_supplied_candidates():
    from core import rules_retrieval as retrieval

    ratings = retrieval.parse_ratings(json.dumps({"results": [
        {"id": number, "score": 0.8, "reason": "Relevant"}
        for number in range(1, 5)
    ]}), 4)

    with pytest.raises(retrieval.RetrievalError):
        retrieval.select_candidates(
            candidates(), ratings, retrieval.RetrievalSettings(before=3, after=2)
        )


@pytest.mark.parametrize("text", [
    '{"query": " CATAN road building cost "}',
    'Поисковый запрос:\n```json\n{"query": " CATAN road building cost "}\n```',
    '{"query": " CATAN road building cost "} пояснение после объекта',
])
def test_query_parser_accepts_model_wrapping_without_changing_search_terms(text):
    from core import rules_retrieval as retrieval

    assert retrieval.parse_query(text) == "CATAN road building cost"


@pytest.mark.parametrize("text", [
    "", "   ", "not JSON", "{}", '{"query": " "}',
    '{"query": 7}', '{"query": null}', '{"query": ["CATAN"]}',
    '{"query": "CATAN"',
])
def test_invalid_query_is_failure_not_an_empty_success(text):
    from core import rules_retrieval as retrieval

    with pytest.raises(retrieval.RetrievalError):
        retrieval.parse_query(text)


@pytest.mark.parametrize("results", [
    [],
    [{"id": 1, "score": 0.8, "reason": "Missing second candidate"}],
    [{"id": 1, "score": 0.8, "reason": "First"}, {"id": 1, "score": 0.9, "reason": "Duplicate"}],
    [{"id": 1, "score": 0.8, "reason": "First"}, {"id": 3, "score": 0.9, "reason": "Unknown"}],
    [{"id": True, "score": 0.8, "reason": "Boolean ID"}, {"id": 2, "score": 0.9, "reason": "Second"}],
    [{"id": 1.0, "score": 0.8, "reason": "Float ID"}, {"id": 2, "score": 0.9, "reason": "Second"}],
    [{"id": 0, "score": 0.8, "reason": "Zero ID"}, {"id": 2, "score": 0.9, "reason": "Second"}],
    ["not a rating", {"id": 2, "score": 0.9, "reason": "Second"}],
])
def test_ratings_must_cover_each_real_candidate_exactly_once(results):
    from core import rules_retrieval as retrieval

    with pytest.raises(retrieval.RetrievalError):
        retrieval.parse_ratings(json.dumps({"results": results}), 2)


@pytest.mark.parametrize("score", [-0.1, 1.1, True, None, "0.8", float("nan"), float("inf")])
def test_invalid_relevance_never_reaches_source_selection(score):
    from core import rules_retrieval as retrieval

    with pytest.raises(retrieval.RetrievalError):
        retrieval.parse_ratings(json.dumps({"results": [
            {"id": 1, "score": score, "reason": "Invalid score"},
        ]}), 1)


@pytest.mark.parametrize("text", [
    "", "not JSON", "{}", '{"results": {}}', '{"results": null}',
    '{"results": [{"id": 1, "score": 0.8}]}',
    '{"results": [{"id": 1, "score": 0.8, "reason": 3}]}',
])
def test_malformed_reranker_output_is_rejected_whole(text):
    from core import rules_retrieval as retrieval

    with pytest.raises(retrieval.RetrievalError):
        retrieval.parse_ratings(text, 1)


def test_empty_ratings_are_valid_only_for_no_candidates():
    from core import rules_retrieval as retrieval

    assert retrieval.parse_ratings('{"results": []}', 0) == ()
    with pytest.raises(retrieval.RetrievalError):
        retrieval.parse_ratings('{"results": []}', 1)


def test_partial_tuning_preserves_other_fields_and_existing_snapshot():
    from core import rules_retrieval as retrieval

    previous = retrieval.RetrievalSettings(before=12, after=4, threshold=0.7)
    changed = retrieval.parse_tuning(previous, "threshold=0.85")

    assert (changed.before, changed.after, changed.threshold) == (12, 4, 0.85)
    assert (previous.before, previous.after, previous.threshold) == (12, 4, 0.7)
    with pytest.raises(FrozenInstanceError):
        previous.threshold = 0.1


def test_tuning_validates_the_final_combination_not_intermediate_values():
    from core import rules_retrieval as retrieval

    previous = retrieval.RetrievalSettings(before=10, after=8)
    changed = retrieval.parse_tuning(previous, "before=3 after=2 threshold=0")

    assert (changed.before, changed.after, changed.threshold) == (3, 2, 0)


@pytest.mark.parametrize("text", [
    "before=2", "before=0", "before=31", "before=1.5", "after=0", "after=11",
    "threshold=-0.1", "threshold=1.1", "threshold=nan", "threshold=inf",
    "after=4 before=3", "before=8 unknown=2", "before=8 before=9",
    "before", "", "threshold=0.7 threshold=0.8",
])
def test_invalid_tuning_cannot_partially_apply_valid_parameters(text):
    from core import rules_retrieval as retrieval

    previous = retrieval.RetrievalSettings(before=10, after=3, threshold=0.6)
    with pytest.raises(retrieval.RetrievalError):
        retrieval.parse_tuning(previous, text)
    assert (previous.before, previous.after, previous.threshold) == (10, 3, 0.6)


@pytest.mark.parametrize("values", [
    {"before": True}, {"before": 1.5}, {"before": 0}, {"before": 31},
    {"after": True}, {"after": 2.0}, {"after": 0}, {"after": 11},
    {"before": 2, "after": 3}, {"threshold": True}, {"threshold": "0.6"},
    {"threshold": float("nan")}, {"threshold": float("inf")}, {"mode": "unknown"},
])
def test_programmatic_settings_cannot_bypass_command_validation(values):
    from core import rules_retrieval as retrieval

    with pytest.raises(retrieval.RetrievalError):
        retrieval.RetrievalSettings(**values)
