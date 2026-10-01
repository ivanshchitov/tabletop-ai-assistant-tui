"""День 24: обязательные источники и цитаты RAG-ответа и режим «не знаю» при слабом контексте.

Ответ, опирающийся на переданные фрагменты правил, обязан называть источник (файл, раздел,
`chunk_id`) и приводить цитаты — дословные фрагменты переданных чанков. Проверка детерминированная
и обращается к модели не больше одного раза на повтор: подтверждённой считается цитата, которая
после нормализации пробелов и регистра не короче минимума и является подстрокой текста хотя бы
одного переданного чанка. Это ловит выдуманную цитату, но не оценивает смысл ответа — смысловую
сверку делает контрольный прогон `docs/rag-citations-questions.md`. Тот же приём, что у инвариантов:
инструкция модели, проверка кодом, один повтор и замена ответа, если повтор тоже не подтверждён.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import List, Optional, Sequence, Tuple

from . import config

from .rules_index import SearchResult

CITATIONS_ASSET = "rules_citations_prompt.md"

# Маркеры нарушений: строки-константы вместо кодов, чтобы журнал и повтор говорили по-русски.
NO_SOURCE_MARKER = "источники не названы"
NO_QUOTE_MARKER = "цитаты не подтверждены текстом переданных фрагментов"

# Текст режима «не знаю»: при релевантности ниже порога приложение отвечает само, без модели.
DISCLAIMER_TEXT = (
    "Не знаю: по этому вопросу в правилах нет фрагментов с достаточной релевантностью, "
    "и я не могу опереться на проверенный источник. Уточните, пожалуйста, вопрос: назовите игру, "
    "редакцию правил или конкретную ситуацию — я поищу снова."
)


@dataclass(frozen=True)
class CitationsCheck:
    """Результат проверки последнего ответа на источники и цитаты.

    `violations` — маркеры нарушений первого ответа, `retried` — был ли повторный запрос,
    `replaced` — заменён ли итог текстом «не знаю», `final_violations` — нарушения повторного
    ответа (пусто, если исходный ответ был подтверждён), `no_context` — сработал режим «не знаю»
    по слабому контексту: запроса к модели не было вовсе.
    """

    violations: Tuple[str, ...] = ()
    retried: bool = False
    replaced: bool = False
    final_violations: Tuple[str, ...] = ()
    no_context: bool = False

    @property
    def confirmed(self) -> bool:
        return not self.violations and not self.replaced


def _normalize(text: str) -> str:
    """Нижний регистр и одиночные пробелы: цитата может отличаться переносами и регистром."""
    return " ".join(text.casefold().split())


def _quotes_in(answer: str, chunks: Sequence[SearchResult]) -> bool:
    """Есть ли подтверждённая цитата: окно чанка не короче минимума, встречающееся в ответе."""
    joined = _normalize(answer)
    for chunk in chunks:
        text = _normalize(chunk.text)
        if len(text) < config.CITATION_MIN_CHARS:
            continue
        # Окно — минимум подряд идущих символов чанка; достаточно, чтобы начало окна встречалось
        # в нормализованном ответе (это и означает, что ответ несёт дословный фрагмент).
        for index in range(len(text) - config.CITATION_MIN_CHARS + 1):
            if index > 0 and text[index - 1] != " ":
                continue
            window = text[index : index + config.CITATION_MIN_CHARS]
            if window in joined:
                return True
    return False


def _has_identifier(answer: str, chunks: Sequence[SearchResult]) -> bool:
    """Назван ли хотя бы один переданный источник: `chunk_id` или файл (не одно название игры).

    Название игры ловит ответ даже без обращения к фрагменту, поэтому идентификатором считается
    только `chunk_id` или имя файла корпуса — они у каждого фрагмента свои.
    """
    text = answer.casefold()
    return any(
        chunk.chunk_id.casefold() in text or chunk.source.casefold() in text for chunk in chunks
    )


def check_answer(answer: str, chunks: Sequence[SearchResult]) -> CitationsCheck:
    """Нарушения обязательных источников и цитат: пусто — ответ подтверждён.

    Пустой список переданных чанков ничего не подтверждает: без фрагментов ответ считался бы
    подтверждённым по умолчанию. Проверка не падает на пустом ответе и пустых фрагментах.
    """
    violations: List[str] = []
    if not chunks or not _has_identifier(answer, chunks):
        violations.append(NO_SOURCE_MARKER)
    if not chunks or not _quotes_in(answer, chunks):
        violations.append(NO_QUOTE_MARKER)
    return CitationsCheck(violations=tuple(violations))


@lru_cache(maxsize=None)
def citations_instruction() -> str:
    """Инструкция цитат из ассета assets/rules_citations_prompt.md."""
    return (config.ASSETS_DIR / CITATIONS_ASSET).read_text(encoding="utf-8").strip()


def citations_message() -> str:
    """Системное сообщение цитат: уходит только при непустых источниках RAG и свободном формате."""
    return citations_instruction()


def retry_prompt(violations: Sequence[str]) -> str:
    """Ход пользователя для повторного запроса: перечень нарушений и требование их исправить."""
    lines = ["Твой ответ не подтверждён источниками RAG:"]
    lines.extend(f"- {violation}" for violation in violations)
    lines.append("")
    lines.append(
        "Перепиши ответ так, чтобы он называл файл, раздел и chunk_id хотя бы одного переданного "
        "фрагмента и содержал дословные цитаты из переданных фрагментов. Если переданные фрагменты "
        "не отвечают на вопрос — скажи, что не знаешь, и попроси уточнить вопрос."
    )
    return "\n".join(lines)


def disclaimer_text() -> str:
    """Фиксированный ответ режима «не знаю»: слабый контекст и просьба уточнить."""
    return DISCLAIMER_TEXT
