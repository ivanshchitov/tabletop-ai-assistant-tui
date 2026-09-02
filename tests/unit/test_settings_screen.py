"""Логика экрана /settings без терминала."""

import pytest

from core import config
from core.answer_settings import AnswerFormat, AnswerSettings
from ui import keyboard, settings_screen
from ui.settings_screen import SettingsScreenState


def press(state: SettingsScreenState, *keys: str) -> SettingsScreenState:
    for key in keys:
        state = settings_screen.apply_key(state, key)
    return state


@pytest.fixture
def state() -> SettingsScreenState:
    return settings_screen.initial_state(AnswerSettings())


# --- начальное состояние -----------------------------------------------------------------


def test_initial_state_mirrors_current_settings():
    settings = AnswerSettings(max_words=123, format=AnswerFormat.JSON, list_limit=4)
    state = settings_screen.initial_state(settings)
    assert state.row == settings_screen.ROW_FORMAT
    assert state.selected_format == AnswerFormat.JSON
    assert state.max_words_input == "123"
    assert state.list_limit_input == "4"


# --- навигация ----------------------------------------------------------------------------


def test_down_moves_through_rows_and_wraps(state):
    assert press(state, keyboard.DOWN).row == settings_screen.ROW_MAX_WORDS
    assert press(state, keyboard.DOWN, keyboard.DOWN).row == settings_screen.ROW_LIST_LIMIT
    assert press(state, keyboard.DOWN, keyboard.DOWN, keyboard.DOWN).row == settings_screen.ROW_FORMAT


def test_up_moves_backwards(state):
    """Стрелка вверх должна идти вверх, а не повторять поведение стрелки вниз."""
    assert press(state, keyboard.UP).row == settings_screen.ROW_LIST_LIMIT
    assert press(state, keyboard.DOWN, keyboard.UP).row == settings_screen.ROW_FORMAT


def test_up_and_down_cancel_each_other(state):
    assert press(state, keyboard.DOWN, keyboard.DOWN, keyboard.UP, keyboard.UP) == state


# --- переключение формата -----------------------------------------------------------------


def test_right_cycles_formats_forward(state):
    values = settings_screen.FORMAT_VALUES
    start = state.format_index
    assert press(state, keyboard.RIGHT).selected_format == values[(start + 1) % len(values)]
    assert press(state, keyboard.RIGHT, keyboard.RIGHT).selected_format == values[
        (start + 2) % len(values)
    ]


def test_format_wraps_around_in_both_directions(state):
    values = settings_screen.FORMAT_VALUES
    forward = press(state, *[keyboard.RIGHT] * len(values))
    assert forward.selected_format == state.selected_format
    backward = press(state, keyboard.LEFT)
    assert backward.selected_format == values[(state.format_index - 1) % len(values)]


def test_format_arrows_ignored_on_numeric_rows(state):
    on_words = press(state, keyboard.DOWN)
    assert press(on_words, keyboard.RIGHT, keyboard.LEFT) == on_words


# --- числовые поля --------------------------------------------------------------------------


def test_digits_append_to_the_selected_field(state):
    result = press(state, keyboard.DOWN, keyboard.BACKSPACE, keyboard.BACKSPACE, keyboard.BACKSPACE, "5", "0")
    assert result.max_words_input == "50"
    assert result.list_limit_input == str(config.DEFAULT_LIST_LIMIT)


def test_digits_go_only_to_the_current_row(state):
    """Цифра, набранная на строке формата, не должна попасть в числовое поле."""
    assert press(state, "7") == state


def test_backspace_erases_one_character(state):
    result = press(state, keyboard.DOWN, keyboard.BACKSPACE)
    assert result.max_words_input == str(config.DEFAULT_MAX_WORDS)[:-1]


def test_field_can_be_emptied_completely(state):
    result = press(state, keyboard.DOWN, *[keyboard.BACKSPACE] * 10)
    assert result.max_words_input == ""


def test_backspace_on_empty_field_is_harmless(state):
    emptied = press(state, keyboard.DOWN, *[keyboard.BACKSPACE] * 10)
    assert press(emptied, keyboard.BACKSPACE) == emptied


def test_list_limit_field_is_edited_independently(state):
    result = press(state, keyboard.DOWN, keyboard.DOWN, keyboard.BACKSPACE, "8")
    assert result.list_limit_input == "8"
    assert result.max_words_input == str(config.DEFAULT_MAX_WORDS)


@pytest.mark.parametrize("key", [keyboard.ENTER, "a", "/", "Ж", "-", "."])
def test_unrelated_keys_do_nothing(state, key):
    on_words = press(state, keyboard.DOWN)
    assert press(on_words, key) == on_words


def test_state_is_immutable(state):
    press(state, keyboard.DOWN, "5")
    assert state.row == settings_screen.ROW_FORMAT
    assert state.max_words_input == str(config.DEFAULT_MAX_WORDS)


# --- применение к настройкам --------------------------------------------------------------


def test_valid_input_is_applied():
    state = press(
        settings_screen.initial_state(AnswerSettings()),
        keyboard.RIGHT,
        keyboard.DOWN,
        *[keyboard.BACKSPACE] * 3,
        "5",
        "0",
        keyboard.DOWN,
        keyboard.BACKSPACE,
        "7",
    )
    settings, errors = settings_screen.apply_to_settings(state, AnswerSettings())
    values = settings_screen.FORMAT_VALUES
    next_format = values[(values.index(AnswerSettings().format) + 1) % len(values)]
    assert errors == []
    assert settings.max_words == 50
    assert settings.list_limit == 7
    assert settings.format == next_format


def test_out_of_range_value_keeps_previous_and_reports():
    """Отказ вместо клампинга: настройка сохраняет старое значение, пользователь видит ошибку."""
    original = AnswerSettings().with_max_words(120)
    state = press(
        settings_screen.initial_state(original),
        keyboard.DOWN,
        *[keyboard.BACKSPACE] * 4,
        "9",
        "9",
        "9",
    )
    settings, errors = settings_screen.apply_to_settings(state, original)
    assert settings.max_words == 120
    assert len(errors) == 1
    assert str(config.MAX_MAX_WORDS) in errors[0]


def test_empty_field_reports_its_own_message():
    original = AnswerSettings()
    state = press(settings_screen.initial_state(original), keyboard.DOWN, *[keyboard.BACKSPACE] * 5)
    settings, errors = settings_screen.apply_to_settings(state, original)
    assert settings.max_words == original.max_words
    assert errors == ["Максимальный объём ответа: введите число слов."]


def test_one_bad_field_does_not_block_the_other():
    original = AnswerSettings(max_words=200, format=AnswerFormat.FREE, list_limit=3)
    state = press(
        settings_screen.initial_state(original),
        keyboard.DOWN,
        *[keyboard.BACKSPACE] * 4,
        keyboard.DOWN,
        keyboard.BACKSPACE,
        "9",
    )
    settings, errors = settings_screen.apply_to_settings(state, original)
    assert settings.list_limit == 9  # валидное поле применилось
    assert settings.max_words == 200  # невалидное осталось прежним
    assert len(errors) == 1


def test_both_fields_can_fail_at_once():
    original = AnswerSettings()
    state = press(
        settings_screen.initial_state(original),
        keyboard.DOWN,
        *[keyboard.BACKSPACE] * 5,
        keyboard.DOWN,
        *[keyboard.BACKSPACE] * 5,
    )
    settings, errors = settings_screen.apply_to_settings(state, original)
    assert len(errors) == 2
    assert settings == original


def test_format_is_applied_even_when_numbers_are_invalid():
    """Формат меняется стрелками и не зависит от валидности числовых полей."""
    original = AnswerSettings()
    state = press(
        settings_screen.initial_state(original),
        keyboard.RIGHT,
        keyboard.DOWN,
        *[keyboard.BACKSPACE] * 5,
    )
    settings, errors = settings_screen.apply_to_settings(state, original)
    values = settings_screen.FORMAT_VALUES
    assert settings.format == values[(values.index(original.format) + 1) % len(values)]
    assert errors


def test_untouched_screen_leaves_settings_unchanged():
    """Открыть /settings и сразу нажать Esc — ничего не должно измениться."""
    original = AnswerSettings(max_words=77, format=AnswerFormat.COMPACT, list_limit=6)
    settings, errors = settings_screen.apply_to_settings(
        settings_screen.initial_state(original), original
    )
    assert settings == original
    assert errors == []


def test_leading_zeros_are_parsed_as_numbers():
    original = AnswerSettings()
    state = press(
        settings_screen.initial_state(original),
        keyboard.DOWN,
        *[keyboard.BACKSPACE] * 5,
        "0",
        "5",
        "0",
    )
    settings, _ = settings_screen.apply_to_settings(state, original)
    assert settings.max_words == 50
