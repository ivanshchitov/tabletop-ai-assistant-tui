"""Инварианты агента: правила, которые он не имеет права нарушать.

Инварианты — не диалог, не память и не профиль: фиксированная таблица в коде (тот же приём, что
таблица правил маршрутизации памяти), которая уходит модели отдельным системным сообщением в каждый
запрос и по которой ответ модели проверяется кодом. Двойная защита: инструкция модели — сверить
просьбу с каждым правилом и отказать, если просьба им противоречит; проверка кодом — запрещённые
слова инварианта в ответе означают нарушение, и такой ответ до пользователя не доходит.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import List, Sequence, Tuple

from . import config

INVARIANTS_PROMPT_ASSET = "invariants_prompt.md"

# Фиксированное начало отказа модели: ответ с этими словами в первых REFUSAL_WINDOW символах —
# отказ, а не решение, и на запрещённые слова не проверяется: отказ обязан назвать, что именно
# запрещено («игры с приложением»), иначе он ничего не объясняет. Окно, а не «начинается с»:
# в JSON-формате отказ лежит внутри {"error": "..."} в блоке кода.
REFUSAL_PREFIX = "Не могу предложить"
REFUSAL_WINDOW = 200


@dataclass(frozen=True)
class Invariant:
    """Одно правило: номер (его называет отказ), текст для модели и запрещённые обороты для кода.

    `forbidden` — основы слов в нижнем регистре («монопол», «приложени»), чтобы ловить падежи
    без морфологии; пустой кортеж — правило проверяет только модель.
    """

    number: int
    rule: str
    forbidden: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Violation:
    """Нарушение инварианта в ответе: номер, правило и оборот таблицы (не фрагмент ответа)."""

    number: int
    rule: str
    term: str


INVARIANTS: Tuple[Invariant, ...] = (
    Invariant(
        1,
        "Только физические компоненты: не предлагать игры с приложениями-компаньонами, смартфонами и планшетами.",
        ("приложени", "смартфон", "планшет"),
    ),
    Invariant(
        2,
        "Партия не дольше двух часов: игры длиннее не предлагать даже как вариант.",
    ),
    Invariant(
        3,
        "«Монополия» под запретом: не советовать и не разбирать её ни в каком виде.",
        ("монопол",),
    ),
    Invariant(
        4,
        "Никаких игр на деньги и азартных игр: покер, казино, ставки.",
        ("покер", "казино", "на деньги"),
    ),
    Invariant(
        5,
        "Не советовать соло-игры и пасьянсы: только игры для двух и более игроков.",
        ("соло", "пасьянс", "одного игрока"),
    ),
    Invariant(
        6,
        "Только официальные правила издателя: хоумрулы и собственные варианты правил не предлагать.",
    ),
)


def is_refusal(answer: str) -> bool:
    """Отказ модели — фиксированное начало отказа в первых REFUSAL_WINDOW символах ответа."""
    return REFUSAL_PREFIX.casefold() in answer[:REFUSAL_WINDOW].casefold()


def check_answer(answer: str, invariants: Sequence[Invariant] = INVARIANTS) -> Tuple[Violation, ...]:
    """Нарушения инвариантов в ответе: по одному на инвариант, с первым найденным оборотом.

    Сравнение регистронезависимое, по вхождению; отказ модели не проверяется — он решения не
    предлагает, а назвать запрещённое в объяснении отказа допустимо.
    """
    if is_refusal(answer):
        return ()
    text = answer.casefold()
    violations: List[Violation] = []
    for invariant in invariants:
        for term in invariant.forbidden:
            if term in text:
                violations.append(Violation(invariant.number, invariant.rule, term))
                break
    return tuple(violations)


@lru_cache(maxsize=None)
def invariants_instruction() -> str:
    """Инструкция работы с инвариантами из ассета assets/invariants_prompt.md."""
    return (config.ASSETS_DIR / INVARIANTS_PROMPT_ASSET).read_text(encoding="utf-8").strip()


@lru_cache(maxsize=None)
def invariants_message() -> str:
    """Системное сообщение с инвариантами для запроса к модели.

    Модели уходят правила с номерами и инструкция; запрещённые слова остаются в коде — список слов
    провоцировал бы обход синонимами вместо соблюдения правила.
    """
    lines: List[str] = ["Инварианты агента — правила, которые нельзя нарушать ни при каких условиях:"]
    lines.extend(f"{inv.number}. {inv.rule}" for inv in INVARIANTS)
    lines.append("")
    lines.append(invariants_instruction())
    return "\n".join(lines)


def _describe(violation: Violation) -> str:
    return f"инвариант {violation.number} «{violation.rule}» (в ответе: «{violation.term}»)"


def refusal_text(violations: Sequence[Violation]) -> str:
    """Отказ приложения вместо отклонённого ответа: называет инвариант и найденное слово."""
    described = "; ".join(_describe(v) for v in violations)
    return f"{REFUSAL_PREFIX}: ответ нарушает {described}."


def retry_prompt(violations: Sequence[Violation]) -> str:
    """Ход пользователя для повторного запроса: перечень нарушений и просьба переписать или отказать."""
    lines = ["Твой ответ нарушает инварианты агента:"]
    lines.extend(f"- {_describe(v)}" for v in violations)
    lines.append("")
    lines.append(
        "Перепиши ответ так, чтобы он не нарушал ни один инвариант, или откажи по инструкции об "
        f"инвариантах (начни с «{REFUSAL_PREFIX}», назови инвариант и объясни причину)."
    )
    return "\n".join(lines)
