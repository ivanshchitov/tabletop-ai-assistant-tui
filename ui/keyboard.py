"""Чтение одиночных нажатий клавиш из терминала (для экрана /settings)."""

import contextlib
import os
import sys

try:
    import select
    import termios
    import tty
except ImportError:  # pragma: no cover - Windows
    select = None
    termios = None
    tty = None

ESC = "ESC"
UP = "UP"
DOWN = "DOWN"
LEFT = "LEFT"
RIGHT = "RIGHT"
ENTER = "ENTER"
BACKSPACE = "BACKSPACE"

_ARROW_CODES = {"A": UP, "B": DOWN, "C": RIGHT, "D": LEFT}


@contextlib.contextmanager
def raw_mode():
    """Переключает stdin в cbreak-режим на время экрана настроек (Unix; на Windows — no-op).

    Режим должен включаться один раз на весь экран, а не на каждый read_key(): если
    переключать его туда-обратно между отдельными нажатиями, байты, пришедшие пачкой
    (несколько Backspace подряд, вставка стрелки), могут застать stdin в canonical-режиме
    между вызовами — тогда драйвер терминала обработает их сам как штатное стирание строки,
    и часть нажатий до приложения не дойдёт.
    """
    if termios is None:
        yield
        return
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def read_key() -> str:
    """Блокирующее чтение одной клавиши внутри `with raw_mode():`. Возвращает спецкод
    (ESC/UP/.../ENTER/BACKSPACE) либо сам символ."""
    if termios is None:
        return _read_key_windows()
    return _read_key_unix()


def _read_key_unix() -> str:
    fd = sys.stdin.fileno()
    # os.read() на самом fd, а не sys.stdin.read(): буферизованный TextIOWrapper может
    # одним system-вызовом забрать из pty сразу все байты escape-последовательности и
    # придержать их в своём внутреннем буфере — тогда select() ниже их не увидит и решит,
    # что байт был всего один (то есть одиночный Esc), а не начало ESC [ A/B/C/D.
    ch = os.read(fd, 1).decode(errors="replace")
    if ch == "\x1b":
        # Стрелки приходят как ESC [ A/B/C/D тремя байтами подряд; одиночный Esc —
        # только один байт, поэтому ждём остаток последовательности с таймаутом.
        if select.select([fd], [], [], 0.05)[0]:
            ch2 = os.read(fd, 1).decode(errors="replace")
            if ch2 == "[" and select.select([fd], [], [], 0.05)[0]:
                ch3 = os.read(fd, 1).decode(errors="replace")
                return _ARROW_CODES.get(ch3, ESC)
        return ESC
    if ch in ("\r", "\n"):
        return ENTER
    if ch in ("\x7f", "\x08"):
        return BACKSPACE
    return ch


def _read_key_windows() -> str:  # pragma: no cover - Windows
    import msvcrt

    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        ch2 = msvcrt.getwch()
        return {"H": UP, "P": DOWN, "K": LEFT, "M": RIGHT}.get(ch2, "")
    if ch == "\x1b":
        return ESC
    if ch == "\r":
        return ENTER
    if ch == "\x08":
        return BACKSPACE
    return ch
