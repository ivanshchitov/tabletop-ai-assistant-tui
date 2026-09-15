"""Профиль пользователя — слой персонализации агента.

Профиль отвечает на вопрос «как отвечать этому человеку», а не «что известно»: его разделы задаёт сам
пользователь в диалоге настройки, и он уходит модели в каждом запросе. Поэтому он и не долговременная
память: та собирается правилами из реплик (`core/memory_layers`), а профиль — объявленное предпочтение,
которое переживает и `/clear`, и перезапуск.

Модуль держит три вещи: модель профиля с разделами, файл профилей (несколько профилей и активный) и
скрипт диалога настройки. Диалог — чистый автомат: `InterviewState` переводит ответ пользователя в
следующий вопрос, а терминальный слой только печатает вопрос и читает строку.
"""

import json
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config

# Разделы профиля: машинные имена — имена полей модели, подписи — для отчёта и сообщения модели.
FIELD_STYLE = "style"
FIELD_CONSTRAINTS = "constraints"
FIELD_EXPERIENCE = "experience"
FIELD_GENRES = "genres"

FIELDS: Tuple[str, ...] = (FIELD_STYLE, FIELD_CONSTRAINTS, FIELD_EXPERIENCE, FIELD_GENRES)

# Пятый вопрос диалога — про имя профиля: оно различает профили в файле, а не описывает пользователя.
FIELD_NAME = "name"

FIELD_LABELS: Dict[str, str] = {
    FIELD_STYLE: "Стиль",
    FIELD_CONSTRAINTS: "Ограничения",
    FIELD_EXPERIENCE: "Опыт",
    FIELD_GENRES: "Жанры и механики",
    # Подпись имени нужна журнальной строке диалога: имя — не раздел профиля, но ответ на первый
    # вопрос диалога отмечается той же строкой, что и разделы.
    FIELD_NAME: "Имя",
}

PROFILE_PROMPT_ASSET = "profile_prompt.md"

NAME_PREFIX = "профиль"

MESSAGE_HEADER_TEMPLATE = "Профиль пользователя «{name}» (персонализация — учитывай в каждом ответе):"

_TRUNCATION_MARK = "…"


def clip_value(value: str, limit: int = config.PROFILE_VALUE_MAX_CHARS) -> str:
    """Обрезает текст раздела до предела длины; многоточие входит в предел."""
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + _TRUNCATION_MARK


@dataclass(frozen=True)
class UserProfile:
    """Профиль пользователя: имя и четыре раздела предпочтений.

    Пустой раздел — нормальное состояние (пользователь мог пропустить вопрос), поэтому профиль
    считается непустым, только когда заполнен хотя бы один раздел: пустой профиль не даёт
    сообщения модели и форма запроса без персонализации не меняется.
    """

    name: str = ""
    style: str = ""
    constraints: str = ""
    experience: str = ""
    genres: str = ""

    def value(self, field_name: str) -> str:
        """Содержимое раздела по машинному имени."""
        return str(getattr(self, field_name))

    def with_value(self, field_name: str, value: str) -> "UserProfile":
        """Копия профиля с заменённым разделом."""
        return replace(self, **{field_name: value})

    @property
    def entries(self) -> Tuple[Tuple[str, str], ...]:
        """Заполненные разделы по порядку: подпись и содержимое."""
        return tuple(
            (FIELD_LABELS[field_name], self.value(field_name))
            for field_name in FIELDS
            if self.value(field_name)
        )

    @property
    def is_empty(self) -> bool:
        """Профиль без единого заполненного раздела — сообщения модели он не даёт."""
        return not self.entries


@dataclass(frozen=True)
class ProfileQuestion:
    """Вопрос диалога настройки: какой раздел он заполняет и что спрашивает."""

    field: str
    prompt: str


# Скрипт диалога: имя профиля и по вопросу на каждый раздел — пять вопросов, больше не нужно.
QUESTIONS: Tuple[ProfileQuestion, ...] = (
    ProfileQuestion(field=FIELD_NAME, prompt="Как вас зовут?"),
    ProfileQuestion(
        field=FIELD_STYLE,
        prompt="Как отвечать: коротко и просто или развёрнуто?",
    ),
    ProfileQuestion(
        field=FIELD_CONSTRAINTS,
        prompt="Что не предлагать и чему следовать (предпочитаемое время партии, сложность, состав)?",
    ),
    ProfileQuestion(
        field=FIELD_EXPERIENCE,
        prompt="Какой у вас опыт в настольных играх (новичок, есть опыт в филлерах, "
        "прожжёный настольщик)?",
    ),
    ProfileQuestion(
        field=FIELD_GENRES,
        prompt="Ваши любимые жанры и механики (евро, амери-трэш, построение движка, кубомёт)?",
    ),
)


@dataclass(frozen=True)
class InterviewState:
    """Состояние диалога настройки: ответы по порядку вопросов.

    Автомат не знает ни о терминале, ни о файле профиля: он отдаёт очередной вопрос и собирает
    профиль из ответов поверх прежнего (пустой ответ оставляет раздел как был).
    """

    answers: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def index(self) -> int:
        """Номер текущего вопроса, с единицы."""
        return len(self.answers) + 1

    @property
    def total(self) -> int:
        return len(QUESTIONS)

    @property
    def finished(self) -> bool:
        return len(self.answers) >= len(QUESTIONS)

    @property
    def question(self) -> ProfileQuestion:
        """Текущий вопрос; на исчерпанном скрипте — последний."""
        return QUESTIONS[min(len(self.answers), len(QUESTIONS) - 1)]

    @property
    def last_field(self) -> str:
        """Раздел, который заполнил последний ответ (для журнальной строки)."""
        return QUESTIONS[len(self.answers) - 1].field if self.answers else ""

    def answer(self, text: str) -> "InterviewState":
        """Принимает ответ и переходит к следующему вопросу."""
        return replace(self, answers=self.answers + (text,))

    def profile(self, base: UserProfile, default_name: str) -> UserProfile:
        """Собирает профиль из ответов поверх прежнего профиля.

        Пустой ответ оставляет раздел как был, поэтому пропущенный вопрос ничего не стирает, а
        повторная настройка под тем же именем — редактирование: заменяются только отвеченные
        разделы. Пустое имя берёт прежнее, а если его нет — свободное имя по порядку.
        """
        profile = base
        for question, answer in zip(QUESTIONS, self.answers):
            value = clip_value(answer)
            if not value:
                continue
            if question.field == FIELD_NAME:
                profile = replace(profile, name=value)
                continue
            profile = profile.with_value(question.field, value)
        if not profile.name:
            profile = replace(profile, name=default_name)
        return profile


class ProfileStore:
    """Файл профилей: несколько профилей и имя активного, чтение при создании, запись после изменения.

    Хранилище отдельное от истории диалога и долговременной памяти: профиль настраивает пользователь,
    и он живёт ровно столько, сколько ему скажет пользователь, — `/clear` его не касается.
    """

    def __init__(self, path: Path = config.PROFILE_FILE):
        self.path = path
        self._profiles: Dict[str, UserProfile] = {}
        self._active: str = ""
        self._load()

    def _load(self) -> None:
        """Читает файл; нет файла, битый JSON или чужая форма — профилей нет, без ошибки."""
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        raw_profiles = data.get("profiles")
        if isinstance(raw_profiles, dict):
            for name, sections in raw_profiles.items():
                if not isinstance(sections, dict):
                    continue
                profile = UserProfile(name=str(name))
                for field_name in FIELDS:
                    value = sections.get(field_name)
                    if isinstance(value, str):
                        profile = profile.with_value(field_name, value)
                self._profiles[str(name)] = profile
        active = data.get("active")
        if isinstance(active, str) and active in self._profiles:
            self._active = active

    @property
    def active_name(self) -> str:
        """Имя активного профиля; пусто — активного профиля нет."""
        return self._active

    def active(self) -> UserProfile:
        """Активный профиль; без активного — пустой профиль."""
        return self._profiles.get(self._active, UserProfile())

    def names(self) -> Tuple[str, ...]:
        """Имена профилей файла по порядку появления."""
        return tuple(self._profiles)

    def get(self, name: str) -> Optional[UserProfile]:
        return self._profiles.get(name)

    def save(self, profile: UserProfile, activate: bool = True) -> None:
        """Записывает профиль под его именем и делает его активным; на диск — сразу."""
        if not profile.name:
            return
        self._profiles[profile.name] = profile
        if activate:
            self._active = profile.name
        self._write()

    def use(self, name: str) -> Optional[UserProfile]:
        """Делает профиль активным; None — такого профиля нет (создаёт профили диалог настройки)."""
        profile = self._profiles.get(name)
        if profile is None:
            return None
        self._active = name
        self._write()
        return profile

    def forget(self, name: str) -> bool:
        """Удаляет профиль; False — такого профиля нет. Удаление активного оставляет файл без активного."""
        if name not in self._profiles:
            return False
        del self._profiles[name]
        if self._active == name:
            self._active = ""
        self._write()
        return True

    def next_name(self) -> str:
        """Свободное имя по порядку — для профиля, названного пустым ответом."""
        index = 1
        while f"{NAME_PREFIX} {index}" in self._profiles:
            index += 1
        return f"{NAME_PREFIX} {index}"

    def _write(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(
                    {
                        "active": self._active,
                        "profiles": {
                            name: {field_name: profile.value(field_name) for field_name in FIELDS}
                            for name, profile in self._profiles.items()
                        },
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError:
            pass


@lru_cache(maxsize=None)
def profile_instruction() -> str:
    """Инструкция работы с профилем из ассета assets/profile_prompt.md."""
    return (config.ASSETS_DIR / PROFILE_PROMPT_ASSET).read_text(encoding="utf-8").strip()


def profile_message(profile: UserProfile) -> "str | None":
    """Системное сообщение с профилем для запроса к модели; пустой профиль сообщения не даёт.

    Разделы печатаются с подписями, чтобы модель различала подачу, ограничения и интересы, а
    инструкция ассета подчиняет предпочтения пользователя настройкам приложения: формат ответа и
    его объём задаёт не профиль.
    """
    if profile.is_empty:
        return None
    lines: List[str] = [MESSAGE_HEADER_TEMPLATE.format(name=profile.name)]
    for label, value in profile.entries:
        lines.append(f"- {label}: {value}")
    lines.append("")
    lines.append(profile_instruction())
    return "\n".join(lines)


def describe_sections() -> Tuple[str, ...]:
    """Разделы профиля текстом — для отчёта интерфейса, без обращения к модели."""
    return tuple(FIELD_LABELS[field_name] for field_name in FIELDS)
