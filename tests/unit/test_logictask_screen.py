"""Редьюсер панели выбора стратегии для /logictask."""

from ui import keyboard
from ui import logictask_screen
from ui.logictask_screen import LogictaskScreenState, initial_state, apply_key


def test_panel_lists_all_four_strategies():
    assert len(logictask_screen.STRATEGY_OPTIONS) == 4
    assert logictask_screen.STRATEGY_OPTIONS[0] == (1, "Прямой ответ")


def test_initial_state_points_to_first_strategy():
    state = initial_state()
    assert state.selected_index == 0
    assert state.selected == logictask_screen.STRATEGY_OPTIONS[0]


def test_down_and_up_move_selection_with_wraparound():
    state = apply_key(initial_state(), keyboard.DOWN)
    assert state.selected_index == 1
    state = apply_key(state, keyboard.UP)
    assert state.selected_index == 0
    state = apply_key(state, keyboard.UP)  # с нуля вверх — на последнюю
    assert state.selected_index == 3
    state = apply_key(state, keyboard.DOWN)  # с последней вниз — на первую
    assert state.selected_index == 0


def test_unrecognized_keys_do_not_change_state():
    state = initial_state()
    for key in (keyboard.LEFT, keyboard.RIGHT, keyboard.BACKSPACE, "5", "x"):
        assert apply_key(state, key) == state


def test_state_is_immutable_dataclass():
    state = LogictaskScreenState()
    assert state.selected_index == 0
