"""Слои памяти агента: что попадает в какой слой, по какому правилу и в каком виде это уходит модели.

Три слоя: краткосрочная память (ход текущего диалога), рабочая (данные текущей задачи) и долговременная
(сведения о пользователе между сессиями). Модуль — чистая логика без I/O: он знает таблицу правил маршрутизации
(какая реплика пользователя в какой слой и под каким ключом пишется), собирает системное сообщение со слоями для
запроса к модели и описывает правила для отчёта интерфейса. Хранилищами владеют агент и `core/long_term_memory`.
"""

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, List, Mapping, Sequence, Tuple

from . import config

# Идентификаторы слоёв: короткие машинные имена, подписи — в LAYER_LABELS.
SHORT_TERM = "short_term"
WORKING = "working"
LONG_TERM = "long_term"

LAYER_LABELS = {
    SHORT_TERM: "краткосрочная",
    WORKING: "рабочая",
    LONG_TERM: "долговременная",
}

# Срок жизни слоя — часть модели памяти, а не деталь отчёта: краткосрочная и рабочая живут текущий
# диалог (команда /clear их опустошает), долговременная — между сессиями и /clear её не трогает.
LAYER_LIFETIME = {
    SHORT_TERM: "текущий диалог, очищается /clear",
    WORKING: "текущая задача, очищается /clear",
    LONG_TERM: "между сессиями, /clear не трогает",
}

# Хранилище слоя — тоже часть модели: слои лежат отдельно друг от друга.
LAYER_STORE_HINT = {
    SHORT_TERM: "конверт истории, блок dialogues",
    WORKING: "конверт истории, блок working",
    LONG_TERM: "отдельный файл долговременной памяти",
}

# Категории записей. У рабочего слоя ключ записи и есть её категория: набор ключей задан таблицей
# правил (цель задачи и ограничения), поэтому отдельного поля в хранилище рабочему слою не нужно.
CATEGORY_GOAL = "цель"
CATEGORY_CONSTRAINT = "ограничения"
CATEGORY_PROFILE = "профиль"
CATEGORY_DECISION = "решения"
CATEGORY_KNOWLEDGE = "знания"
CATEGORY_NOTE = "заметка"

MEMORY_PROMPT_ASSET = "memory_prompt.md"

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")

MESSAGE_HEADER = "Память ассистента (слои памяти, от долговременной к текущей задаче):"

_LAYER_HEADER = {
    LONG_TERM: "Долговременная память (сведения о пользователе между сессиями):",
    WORKING: "Рабочая память (данные текущей задачи этого диалога):",
}

_TRUNCATION_MARK = "…"


def clip_value(value: str, limit: int = config.MEMORY_VALUE_MAX_CHARS) -> str:
    """Обрезает значение до предела длины записи; многоточие входит в предел.

    Записи уходят в каждый запрос, поэтому длинная реплика в слой не помещается целиком.
    """
    if len(value) <= limit:
        return value
    return value[: limit - 1] + _TRUNCATION_MARK


@dataclass(frozen=True)
class MemoryRecord:
    """Запись слоя памяти: слой, категория, ключ и значение.

    Ключ задан правилом маршрутизации или командой пользователя; повторная запись по тому же ключу
    заменяет значение, а не добавляет вторую запись.
    """

    layer: str
    category: str
    key: str
    value: str


@dataclass(frozen=True)
class MemoryRule:
    """Правило маршрутизации: набор оборотов реплики, слой, категория и ключ записи.

    `patterns` — регулярные выражения (ищутся по реплике без учёта регистра, поэтому кириллица
    распознаётся в любом регистре). Порядок шаблонов значим: значение берётся по первому
    сработавшему.
    """

    layer: str
    category: str
    key: str
    patterns: Tuple[str, ...]
    description: str


# Порядок правил значим: записи собираются в словарь по ключу, поэтому правило, идущее ниже,
# заменяет значение ключа, записанное выше. Внутри правила первым берётся первый сработавший шаблон.
RULES: Tuple[MemoryRule, ...] = (
    MemoryRule(
        layer=LONG_TERM,
        category=CATEGORY_PROFILE,
        key="опыт",
        patterns=(
            r"я\s+(?:—\s*)?опытн\w*\s+игрок",
            r"\bновичок\b",
            r"играю\s+(?:давно|много\s+лет)",
            r"у\s+меня\s+[^.!?\n]{0,40}?парти",
        ),
        description="опыт в настольных играх",
    ),
    MemoryRule(
        layer=LONG_TERM,
        category=CATEGORY_PROFILE,
        key="предпочтения",
        patterns=(
            r"я\s+не\s+люблю",
            r"не\s+люблю",
            r"предпочитаю",
            r"мне\s+нравится",
        ),
        description="предпочтения игрока",
    ),
    MemoryRule(
        layer=LONG_TERM,
        category=CATEGORY_PROFILE,
        key="группа",
        patterns=(
            r"моя\s+группа",
            r"мои\s+игроки",
            r"наша\s+компания",
            r"мы\s+играем",
        ),
        description="состав постоянной группы",
    ),
    MemoryRule(
        layer=LONG_TERM,
        category=CATEGORY_DECISION,
        key="решения",
        patterns=(
            r"\bрешили\b",
            r"\bдоговорились\b",
            r"\bзапомни\b",
            r"\bвпредь\b",
        ),
        description="принятые решения и договорённости",
    ),
    MemoryRule(
        layer=LONG_TERM,
        category=CATEGORY_KNOWLEDGE,
        key="знания",
        patterns=(
            r"у\s+нас\s+принято",
            r"в\s+нашей\s+компании\s+принято",
            r"домашни[ех]\s+правил",
            r"\bхоумрул",
        ),
        description="знания о том, как играют в группе пользователя",
    ),
    MemoryRule(
        layer=WORKING,
        category=CATEGORY_GOAL,
        key=CATEGORY_GOAL,
        patterns=(
            r"\bхочу\b",
            r"\bподбери\w*",
            r"\bподобрать\b",
            r"\bсобери\w*",
            r"\bсобрать\b",
            r"\bпосоветуй\w*",
            r"\bпомоги\w*",
            r"\bищем\b",
            r"\bнужна\s+игра\b",
        ),
        description="текущая цель задачи",
    ),
    MemoryRule(
        layer=WORKING,
        category=CATEGORY_CONSTRAINT,
        key=CATEGORY_CONSTRAINT,
        patterns=(
            r"\bне\s+предлагай\w*",
            r"\bне\s+более\b",
            r"\bне\s+больше\b",
            r"\bне\s+меньше\b",
            r"\bисключи\w*",
            r"\bбюджет\b",
            r"\bдо\s+\d+\s*(?:₽|руб)",
            r"\b\d+\s+человек",
            # «без …» ограничено темами игр: свободное «без \w+» ловило бы любую реплику
            # («вопрос без ответа») и писало бы в рабочую память случайный текст.
            r"\bбез\s+(?:таймер|филлер|кооператив|торгов|конфликт|рандом|дополнени|сложн)\w*",
        ),
        description="ограничения текущей задачи",
    ),
)


def working_record(key: str, value: str) -> MemoryRecord:
    """Запись рабочего слоя: категорией служит сам ключ, набор которого задан правилами."""
    return MemoryRecord(layer=WORKING, category=key, key=key, value=value)


def records_from_block(layer: str, block: Mapping[str, str]) -> Tuple[MemoryRecord, ...]:
    """Записи слоя из плоского блока «ключ — значение» его хранилища (рабочая память history.json)."""
    return tuple(working_record(key, value) for key, value in block.items())


def _sentence_with(text: str, match: "re.Match[str]") -> str:
    """Предложение реплики, в котором сработал оборот, — оно и становится значением записи."""
    start = max(text.rfind(separator, 0, match.start()) for separator in ".!?…\n")
    sentences = _SENTENCE_SPLIT_RE.split(text[start + 1 :])
    offset = start + 1
    for sentence in sentences:
        end = offset + len(sentence)
        if match.start() <= end or offset <= match.start() < end:
            return sentence.strip().rstrip(".!?…")
        offset = end + 1
    return text.strip().rstrip(".!?…")


def route(message: str) -> Tuple[MemoryRecord, ...]:
    """Разбирает реплику пользователя правилами и возвращает записи слоёв по одной на ключ.

    Ни одного запроса к модели: решение принимает таблица правил, поэтому маршрут детерминирован,
    не тратит токены и не зависит от ответа модели. Реплика без распознанных оборотов не даёт
    записей — она остаётся только в краткосрочном слое как ход диалога.
    """
    found: Dict[str, MemoryRecord] = {}
    for rule in RULES:
        matched: "re.Match[str] | None" = None
        for pattern in rule.patterns:
            matched = re.search(pattern, message, re.IGNORECASE)
            if matched is not None:
                break
        if matched is None:
            continue
        found[rule.key] = MemoryRecord(
            layer=rule.layer,
            category=rule.category,
            key=rule.key,
            value=clip_value(_sentence_with(message, matched)),
        )
    return tuple(found.values())


@lru_cache(maxsize=None)
def memory_instruction() -> str:
    """Инструкция работы со слоями памяти из ассета assets/memory_prompt.md."""
    return (config.ASSETS_DIR / MEMORY_PROMPT_ASSET).read_text(encoding="utf-8").strip()


def memory_message(records: Sequence[MemoryRecord]) -> "str | None":
    """Системное сообщение со слоями памяти для запроса к модели; пустые слои сообщения не дают.

    Слои печатаются от долговременной памяти к рабочей, а записи — с меткой категории: модель должна
    различать сведения о пользователе и данные текущей задачи, чтобы свежая реплика могла их
    перевесить. Инструкция взаимодействия со слоями — в ассете.
    """
    if not records:
        return None
    lines: List[str] = [MESSAGE_HEADER]
    for layer in (LONG_TERM, WORKING):
        layer_records = [record for record in records if record.layer == layer]
        if not layer_records:
            continue
        lines.append("")
        lines.append(_LAYER_HEADER[layer])
        for record in layer_records:
            lines.append(f"- {record.category} · {record.key}: {record.value}")
    lines.append("")
    lines.append(memory_instruction())
    return "\n".join(lines)


def describe_rules() -> Tuple[str, ...]:
    """Правила маршрутизации текстом — для отчёта интерфейса, без обращения к модели."""
    described: List[str] = []
    for rule in RULES:
        phrases = ", ".join(f"«{pattern}»" for pattern in rule.patterns)
        described.append(
            f"{phrases} → {LAYER_LABELS[rule.layer]} память "
            f"({rule.category} · {rule.key}): {rule.description}"
        )
    return tuple(described)
