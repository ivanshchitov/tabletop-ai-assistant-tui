"""Состояние панели выбора стратегии /logictask, отделённое от терминала.

Схема та же, что у `/settings`: вся реакция на клавиши живёт здесь — в чистом редьюсере,
который тестируется без псевдотерминала; `ui/tui_app.py` остаётся только цикл
read-key/redraw и запуск выбранной стратегии.
"""

from dataclasses import dataclass, replace
from typing import List, Tuple

from core.logictask import STRATEGY_HEADERS

from . import keyboard

STRATEGY_OPTIONS: List[Tuple[int, str]] = list(STRATEGY_HEADERS)


@dataclass(frozen=True)
class LogictaskScreenState:
    """Снимок панели выбора: курсор на одной из четырёх стратегий."""

    selected_index: int = 0

    @property
    def selected(self) -> Tuple[int, str]:
        return STRATEGY_OPTIONS[self.selected_index]


def initial_state() -> LogictaskScreenState:
    return LogictaskScreenState(selected_index=0)


def apply_key(state: LogictaskScreenState, key: str) -> LogictaskScreenState:
    if key in (keyboard.UP, keyboard.DOWN):
        step = -1 if key == keyboard.UP else 1
        return replace(state, selected_index=(state.selected_index + step) % len(STRATEGY_OPTIONS))
    return state
