"""Слои памяти: правила маршрутизации, сборка сообщения памяти и снимок."""

from core import config, memory_layers


def long_term(key, value, category=memory_layers.CATEGORY_PROFILE):
    return memory_layers.MemoryRecord(
        layer=memory_layers.LONG_TERM, category=category, key=key, value=value
    )


def test_question_without_turns_writes_nothing():
    assert memory_layers.route("Какие правила у Каркассона?") == ()


def test_experience_goes_to_long_term_profile():
    records = memory_layers.route("Я опытный игрок, у меня больше 300 партий в Каркассон.")

    assert len(records) == 1
    record = records[0]
    assert record.layer == memory_layers.LONG_TERM
    assert record.category == memory_layers.CATEGORY_PROFILE
    assert record.key == "опыт"
    assert "опытный игрок" in record.value


def test_preference_goes_to_long_term_profile():
    records = memory_layers.route("Я не люблю игры, где нужно торговаться.")

    assert [record.key for record in records] == ["предпочтения"]
    assert records[0].category == memory_layers.CATEGORY_PROFILE


def test_decision_goes_to_long_term():
    records = memory_layers.route("Решили, что больше не берём игры с таймером.")

    assert [record.key for record in records] == ["решения"]
    assert records[0].layer == memory_layers.LONG_TERM
    assert records[0].category == memory_layers.CATEGORY_DECISION


def test_house_rule_goes_to_long_term_knowledge():
    records = memory_layers.route("В нашей компании принято считать очки по домашним правилам.")

    assert [record.key for record in records] == ["знания"]
    assert records[0].category == memory_layers.CATEGORY_KNOWLEDGE


def test_goal_and_constraints_go_to_working_layer():
    records = memory_layers.route(
        "Собери компанию из 4 человек на вечер, не предлагай игры с таймером."
    )

    by_key = {record.key: record for record in records}
    assert set(by_key) == {"цель", "ограничения"}
    assert all(record.layer == memory_layers.WORKING for record in records)
    assert by_key["цель"].category == memory_layers.CATEGORY_GOAL
    assert by_key["ограничения"].category == memory_layers.CATEGORY_CONSTRAINT


def test_unrelated_phrase_does_not_route_anything():
    """Свободные обороты не должны попадать в слои: правило ловит темы, а не любое слово."""
    assert memory_layers.route("Вопрос без ответа") == ()
    assert memory_layers.route("Что нового в дополнении к Каркассону?") == ()


def test_constraint_phrase_about_games_still_routes():
    records = memory_layers.route("Собери партию без таймера.")

    assert [record.key for record in records] == ["цель", "ограничения"]


def test_recognition_is_case_insensitive_and_inside_sentence():
    records = memory_layers.route("Слушай, РЕШИЛИ взять на вечер Диксит.")

    assert [record.key for record in records] == ["решения"]


def test_value_is_the_sentence_with_the_match():
    records = memory_layers.route(
        "Люблю тяжёлые евро. Собери партию на вечер, не предлагай филлеры."
    )

    by_key = {record.key: record.value for record in records}
    assert by_key["цель"] == "Собери партию на вечер, не предлагай филлеры"
    assert by_key["ограничения"] == "Собери партию на вечер, не предлагай филлеры"


def test_value_is_truncated_to_the_limit():
    phrase = "Собери " + "очень длинный запрос " * 20
    records = memory_layers.route(phrase)

    assert len(records) == 1
    assert len(records[0].value) == config.MEMORY_VALUE_MAX_CHARS
    assert records[0].value.endswith("…")


def test_one_record_per_key_with_the_first_matching_pattern():
    records = memory_layers.route("Хочу игру на вечер, не предлагай филлеры, не более 30 минут.")

    constraints = [record for record in records if record.key == "ограничения"]
    assert len(constraints) == 1


def test_message_is_empty_when_layers_are_empty():
    assert memory_layers.memory_message(()) is None


def test_long_term_memory_goes_before_working():
    message = memory_layers.memory_message(
        (
            memory_layers.working_record("цель", "Собрать партию на вечер"),
            long_term("опыт", "Я опытный игрок"),
        )
    )

    assert message is not None
    assert message.index("опытный игрок") < message.index("Собрать партию на вечер")
    assert memory_layers.CATEGORY_PROFILE in message
    assert "цель" in message


def test_message_carries_the_instruction_asset():
    message = memory_layers.memory_message((memory_layers.working_record("цель", "Собрать партию"),))

    assert memory_layers.memory_instruction()[:30] in message


def test_message_marks_categories_of_long_term_records():
    message = memory_layers.memory_message(
        (
            long_term("опыт", "много партий"),
            long_term("решения", "без таймера", memory_layers.CATEGORY_DECISION),
        )
    )

    assert memory_layers.CATEGORY_PROFILE in message
    assert memory_layers.CATEGORY_DECISION in message


def test_rules_description_covers_every_rule():
    described = memory_layers.describe_rules()

    assert len(described) == len(memory_layers.RULES)
    assert any(memory_layers.LAYER_LABELS[memory_layers.LONG_TERM] in line for line in described)
    assert any(memory_layers.LAYER_LABELS[memory_layers.WORKING] in line for line in described)


def test_records_from_block_keep_the_stored_values():
    records = memory_layers.records_from_block(
        memory_layers.WORKING, {"цель": "Собрать партию", "ограничения": "без таймера"}
    )

    assert {record.key for record in records} == {"цель", "ограничения"}
    assert all(record.layer == memory_layers.WORKING for record in records)
    assert all(record.value for record in records)
