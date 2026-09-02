"""Состояние экрана /settings, отделённое от терминала.

Здесь живёт вся логика реакции на клавиши и применения введённых значений к
`AnswerSettings`. Отрисовка (`rich.Panel`) и чтение клавиш остаются в `ui/tui_app.py` и
`ui/keyboard.py` — так поведение экрана проверяется обычными тестами, без псевдотерминала.
"""

from dataclasses import dataclass, replace
from typing import List, Tuple

from core.answer_settings import AnswerFormat, AnswerSettings, AnswerSettingsError

from . import keyboard

ROW_FORMAT = 0
ROW_MAX_WORDS = 1
ROW_LIST_LIMIT = 2
ROWS_COUNT = 3

FORMAT_VALUES: List[AnswerFormat] = list(AnswerFormat)


@dataclass(frozen=True)
class SettingsScreenState:
    """Снимок экрана настроек: выбранная строка, позиция формата и содержимое числовых полей.

    Числовые поля хранятся строками, а не числами: пользователь может стереть поле в пустоту
    или временно набрать значение вне допустимого диапазона, и экран обязан это показывать —
    к числу и к диапазону они приводятся только на выходе, в `apply_to_settings`.
    """

    row: int = ROW_FORMAT
    format_index: int = 0
    max_words_input: str = ""
    list_limit_input: str = ""

    @property
    def selected_format(self) -> AnswerFormat:
        return FORMAT_VALUES[self.format_index]


def initial_state(settings: AnswerSettings) -> SettingsScreenState:
    return SettingsScreenState(
        row=ROW_FORMAT,
        format_index=FORMAT_VALUES.index(settings.format),
        max_words_input=str(settings.max_words),
        list_limit_input=str(settings.list_limit),
    )


def apply_key(state: SettingsScreenState, key: str) -> SettingsScreenState:
    """Возвращает новое состояние экрана после нажатия клавиши.

    Нераспознанные клавиши (и клавиши, неприменимые к текущей строке) возвращают состояние
    без изменений — экран не должен реагировать, например, на цифры на строке формата.
    """
    if key in (keyboard.UP, keyboard.DOWN):
        step = -1 if key == keyboard.UP else 1
        return replace(state, row=(state.row + step) % ROWS_COUNT)

    if state.row == ROW_FORMAT and key in (keyboard.LEFT, keyboard.RIGHT):
        step = -1 if key == keyboard.LEFT else 1
        return replace(state, format_index=(state.format_index + step) % len(FORMAT_VALUES))

    if state.row == ROW_MAX_WORDS:
        if key == keyboard.BACKSPACE:
            return replace(state, max_words_input=state.max_words_input[:-1])
        if len(key) == 1 and key.isdigit():
            return replace(state, max_words_input=state.max_words_input + key)

    if state.row == ROW_LIST_LIMIT:
        if key == keyboard.BACKSPACE:
            return replace(state, list_limit_input=state.list_limit_input[:-1])
        if len(key) == 1 and key.isdigit():
            return replace(state, list_limit_input=state.list_limit_input + key)

    return state


def apply_to_settings(
    state: SettingsScreenState, settings: AnswerSettings
) -> Tuple[AnswerSettings, List[str]]:
    """Применяет состояние экрана к настройкам.

    Возвращает новые настройки и список сообщений об ошибках. Невалидное поле (пустое или вне
    диапазона) не применяется, но и не отменяет остальные: настройка сохраняет прежнее значение,
    а вызывающий код показывает сообщение — это осознанный отказ от тихого клампинга.
    """
    errors: List[str] = []
    result = settings.with_format(state.selected_format)

    if state.max_words_input.isdigit():
        try:
            result = result.with_max_words(int(state.max_words_input))
        except AnswerSettingsError as exc:
            errors.append(str(exc))
    else:
        errors.append("Максимальный объём ответа: введите число слов.")

    if state.list_limit_input.isdigit():
        try:
            result = result.with_list_limit(int(state.list_limit_input))
        except AnswerSettingsError as exc:
            errors.append(str(exc))
    else:
        errors.append("Лимит вариантов в списке: введите число.")

    return result, errors
