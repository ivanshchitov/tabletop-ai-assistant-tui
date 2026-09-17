"""Инварианты агента: фиксированная таблица, проверка ответа кодом, сообщение модели и тексты отказа."""

import pytest

from core import config
from core.invariants import (
    INVARIANTS,
    REFUSAL_PREFIX,
    REFUSAL_WINDOW,
    Invariant,
    Violation,
    check_answer,
    invariants_message,
    is_refusal,
    refusal_text,
    retry_prompt,
)

# --- таблица ---


def test_table_has_six_numbered_invariants_with_rules():
    """Инварианты заданы заранее и пронумерованы по порядку: номер — то, что называет отказ."""
    assert len(INVARIANTS) == 6
    assert [inv.number for inv in INVARIANTS] == [1, 2, 3, 4, 5, 6]
    assert all(isinstance(inv, Invariant) and inv.rule.strip() for inv in INVARIANTS)


def test_four_invariants_carry_forbidden_terms_and_two_are_model_only():
    """Двойная защита там, где нарушение видно в тексте; остальное держит только промпт."""
    with_terms = [inv for inv in INVARIANTS if inv.forbidden]
    model_only = [inv for inv in INVARIANTS if not inv.forbidden]
    assert [inv.number for inv in with_terms] == [1, 3, 4, 5]
    assert [inv.number for inv in model_only] == [2, 6]
    assert all(term == term.casefold() for inv in with_terms for term in inv.forbidden)


# --- проверка ответа: по положительному и отрицательному примеру на каждый инвариант со словами ---


@pytest.mark.parametrize(
    "number, violating, clean",
    [
        (1, "Скачайте ПРИЛОЖЕНИЕ-компаньон на смартфон.", "Только карты, кубики и поле — ничего лишнего."),
        (3, "Возьмите «Монополию» — классика.", "Возьмите «Каркассон» — классика."),
        (4, "Сыграйте в покер на деньги.", "Сыграйте в «Кодовые имена» на очки."),
        (5, "Есть отличный пасьянс для одного игрока.", "Есть отличная игра для четырёх игроков."),
    ],
)
def test_each_invariant_with_terms_catches_its_violation_and_passes_clean_text(number, violating, clean):
    numbers = {v.number for v in check_answer(violating)}
    assert numbers == {number}
    assert check_answer(clean) == ()


def test_violation_carries_number_rule_and_table_term():
    """Оборот из таблицы, не фрагмент ответа: стабильный текст для журнала."""
    (violation,) = check_answer("Лучше всего зайдёт МоНоПоЛиЯ.")
    assert violation == Violation(number=3, rule=INVARIANTS[2].rule, term="монопол")


def test_two_terms_of_different_invariants_give_two_violations():
    violations = check_answer("Откройте приложение и сыграйте в монополию.")
    assert [v.number for v in violations] == [1, 3]


def test_one_invariant_reports_a_single_violation_even_with_several_terms():
    violations = check_answer("Смартфон и планшет обязательны.")
    assert [v.number for v in violations] == [1]
    assert violations[0].term == "смартфон"


def test_clean_answer_has_no_violations():
    assert check_answer("🎲 Возьмите «Диксит»: 3–6 игроков, 30 минут.") == ()


# --- отказ модели ---


def test_refusal_prefix_within_window_skips_the_check():
    """Отказ называет запрещённое («игры с приложением») и решения не предлагает."""
    answer = f"{REFUSAL_PREFIX}: просьба противоречит инварианту 1 — игры с приложением на смартфоне запрещены."
    assert is_refusal(answer)
    assert check_answer(answer) == ()


def test_refusal_inside_json_block_is_still_a_refusal():
    answer = '```json\n{\n  "error": "Не могу предложить: инвариант 3 запрещает Монополию"\n}\n```'
    assert is_refusal(answer)
    assert check_answer(answer) == ()


def test_refusal_prefix_is_case_insensitive():
    assert is_refusal("НЕ МОГУ ПРЕДЛОЖИТЬ такое: приложение под запретом.")


def test_refusal_phrase_beyond_the_window_does_not_protect_a_solution():
    answer = "Вот игра с приложением: " + "и ещё много слов " * 20 + f"{REFUSAL_PREFIX} иначе."
    assert len(answer) > REFUSAL_WINDOW
    assert not is_refusal(answer)
    assert [v.number for v in check_answer(answer)] == [1]


# --- сообщение модели и тексты ---


def test_message_lists_every_rule_with_its_number_and_the_instruction():
    message = invariants_message()
    for inv in INVARIANTS:
        assert f"{inv.number}. {inv.rule}" in message
    assert REFUSAL_PREFIX in message
    assert "профил" in message.casefold()
    assert (config.ASSETS_DIR / "invariants_prompt.md").read_text(encoding="utf-8").strip() in message


def test_message_does_not_hand_the_forbidden_terms_to_the_model():
    """Модели уходят правила; список слов провоцировал бы обход синонимами, а не соблюдение."""
    message = invariants_message()
    assert "запрещённые слова" not in message.casefold()
    assert "монопол," not in message.casefold()
    assert "приложени," not in message.casefold()


def test_refusal_text_names_number_rule_and_term():
    text = refusal_text(check_answer("Сыграйте в монополию."))
    assert text.startswith(REFUSAL_PREFIX)
    assert "инвариант 3" in text
    assert INVARIANTS[2].rule in text
    assert "«монопол»" in text
    assert is_refusal(text)


def test_retry_prompt_lists_violations_and_asks_to_rewrite_or_refuse():
    text = retry_prompt(check_answer("Откройте приложение и сыграйте в монополию."))
    assert "инвариант 1" in text and "инвариант 3" in text
    assert "«приложени»" in text and "«монопол»" in text
    assert "перепиши" in text.casefold()
    assert "откажи" in text.casefold()
