"""Редьюсер панели выбора команды для /commands."""

from ui import keyboard
from ui import commands_screen
from ui.commands_screen import CommandsScreenState, initial_state, apply_key


def test_panel_lists_all_commands_with_descriptions():
    commands = [command for command, _ in commands_screen.COMMAND_OPTIONS]
    assert commands == ["/exit", "/commands", "/settings", "/clear", "/logictask"]
    for command, description in commands_screen.COMMAND_OPTIONS:
        assert command.startswith("/")
        assert description


def test_initial_state_points_to_first_command_and_not_finished():
    state = initial_state()
    assert state.selected_index == 0
    assert state.selected == commands_screen.COMMAND_OPTIONS[0]
    assert not state.confirmed
    assert not state.cancelled


def test_down_and_up_move_selection_with_wraparound():
    state = apply_key(initial_state(), keyboard.DOWN)
    assert state.selected_index == 1
    state = apply_key(state, keyboard.UP)
    assert state.selected_index == 0
    state = apply_key(state, keyboard.UP)  # с нуля вверх — на последнюю
    assert state.selected_index == len(commands_screen.COMMAND_OPTIONS) - 1
    state = apply_key(state, keyboard.DOWN)  # с последней вниз — на первую
    assert state.selected_index == 0


def test_enter_confirms_selection():
    state = apply_key(initial_state(), keyboard.DOWN)
    state = apply_key(state, keyboard.ENTER)
    assert state.confirmed
    assert not state.cancelled
    assert state.selected == commands_screen.COMMAND_OPTIONS[1]


def test_esc_cancels_panel():
    state = apply_key(initial_state(), keyboard.ESC)
    assert state.cancelled
    assert not state.confirmed


def test_keys_after_finish_do_not_change_state():
    confirmed = apply_key(initial_state(), keyboard.ENTER)
    cancelled = apply_key(initial_state(), keyboard.ESC)
    for state in (confirmed, cancelled):
        assert apply_key(state, keyboard.DOWN) == state
        assert apply_key(state, keyboard.UP) == state
        assert apply_key(state, keyboard.ENTER) == state
        assert apply_key(state, keyboard.ESC) == state


def test_unrecognized_keys_do_not_change_state():
    state = initial_state()
    for key in (keyboard.LEFT, keyboard.RIGHT, keyboard.BACKSPACE, "5", "x"):
        assert apply_key(state, key) == state


def test_state_is_immutable_dataclass():
    state = CommandsScreenState()
    assert state.selected_index == 0
    assert not state.confirmed
    assert not state.cancelled
