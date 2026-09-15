"""Профиль пользователя: хранилище профилей, диалог настройки и сообщение для модели."""

import importlib
import json

from core import config
from core.user_profile import (
    FIELD_GENRES,
    FIELD_NAME,
    FIELD_STYLE,
    FIELDS,
    QUESTIONS,
    InterviewState,
    ProfileStore,
    UserProfile,
    profile_message,
)


def test_profile_file_paths_and_limit(monkeypatch, tmp_path):
    """Путь и предел длины — часть конфигурации: без переключателя прогон писал бы реальный файл."""
    assert config.PROFILE_FILE == config.BASE_DIR / "profile.json"
    assert 100 <= config.PROFILE_VALUE_MAX_CHARS <= 2000

    monkeypatch.setenv("TABLETOP_PROFILE_FILE", str(tmp_path / "other-profile.json"))
    try:
        assert importlib.reload(config).PROFILE_FILE == tmp_path / "other-profile.json"
    finally:
        monkeypatch.delenv("TABLETOP_PROFILE_FILE", raising=False)
        importlib.reload(config)


def _store(tmp_path) -> ProfileStore:
    return ProfileStore(tmp_path / "profile.json")


def _profile(name: str = "новичок", **sections: str) -> UserProfile:
    return UserProfile(name=name, **sections)


# --- хранилище ---


def test_sections_land_in_the_file_immediately(tmp_path):
    store = _store(tmp_path)
    store.save(_profile(style="коротко и просто"))

    saved = json.loads((tmp_path / "profile.json").read_text(encoding="utf-8"))
    assert saved["profiles"]["новичок"]["style"] == "коротко и просто"
    assert saved["active"] == "новичок"


def test_several_profiles_live_in_one_file_with_the_active_one(tmp_path):
    store = _store(tmp_path)
    store.save(_profile("новичок", style="коротко"))
    store.save(_profile("эксперт", style="развёрнуто"))

    assert store.names() == ("новичок", "эксперт")
    assert store.active_name == "эксперт"
    assert store.active().style == "развёрнуто"


def test_using_an_unknown_profile_does_not_create_it(tmp_path):
    store = _store(tmp_path)
    store.save(_profile("новичок", style="коротко"))

    assert store.use("эксперт") is None
    assert store.names() == ("новичок",)
    assert store.active_name == "новичок"


def test_using_a_known_profile_switches_the_active_one(tmp_path):
    store = _store(tmp_path)
    store.save(_profile("новичок", style="коротко"))
    store.save(_profile("эксперт", style="развёрнуто"))

    switched = store.use("новичок")

    assert switched is not None and switched.name == "новичок"
    assert store.active_name == "новичок"
    assert store.active().style == "коротко"


def test_forget_removes_only_the_named_profile(tmp_path):
    store = _store(tmp_path)
    store.save(_profile("новичок", style="коротко"))
    store.save(_profile("эксперт", style="развёрнуто"))

    assert store.forget("эксперт") is True

    assert store.names() == ("новичок",)
    assert store.forget("эксперт") is False


def test_forgetting_the_active_profile_leaves_no_active_profile(tmp_path):
    store = _store(tmp_path)
    store.save(_profile("новичок", style="коротко"))

    store.forget("новичок")

    assert store.active_name == ""
    assert store.active().is_empty


def test_missing_and_broken_file_read_as_no_profiles(tmp_path):
    missing = _store(tmp_path)
    assert missing.names() == () and missing.active_name == ""

    broken_path = tmp_path / "broken.json"
    broken_path.write_text("{не json", encoding="utf-8")
    broken = ProfileStore(broken_path)
    assert broken.names() == () and broken.active().is_empty


def test_next_name_skips_taken_names(tmp_path):
    store = _store(tmp_path)
    store.save(_profile("профиль 1", style="коротко"))

    assert store.next_name() == "профиль 2"


def test_write_error_does_not_break_the_session(tmp_path):
    store = ProfileStore(tmp_path)  # каталог вместо файла: запись невозможна

    store.save(_profile("новичок", style="коротко"))

    assert store.active().style == "коротко"


# --- диалог настройки ---


def test_interview_asks_five_questions_covering_all_sections():
    assert len(QUESTIONS) <= 5
    assert [question.field for question in QUESTIONS] == [FIELD_NAME, *FIELDS]


def test_interview_asks_one_question_at_a_time():
    state = InterviewState()

    assert state.question.field == FIELD_NAME
    assert len(QUESTIONS) - len(state.answers) == 5

    state = state.answer("новичок")

    assert state.question.field == FIELD_STYLE


def test_answers_become_the_sections_of_the_profile():
    state = InterviewState()
    for answer in ("новичок", "коротко", "до часа", "новичок", "евро"):
        state = state.answer(answer)

    profile = state.profile(UserProfile(), "профиль 1")

    assert state.finished is True
    assert profile.name == "новичок"
    assert (profile.style, profile.constraints, profile.experience, profile.genres) == (
        "коротко",
        "до часа",
        "новичок",
        "евро",
    )


def test_empty_answer_keeps_the_section_as_it_was():
    base = _profile("новичок", style="коротко и просто", constraints="до часа")
    state = InterviewState()

    for answer in ("", "", "без таймеров", "", ""):
        state = state.answer(answer)
    profile = state.profile(base, "профиль 1")

    assert profile.name == "новичок"
    assert profile.style == "коротко и просто"
    assert profile.constraints == "без таймеров"
    assert profile.experience == ""


def test_empty_name_gives_a_free_default_name():
    state = InterviewState()
    for _ in range(len(QUESTIONS)):
        state = state.answer("")

    assert state.profile(UserProfile(), "профиль 3").name == "профиль 3"


def test_repeat_setup_replaces_only_the_answered_sections():
    base = _profile("новичок", style="коротко", constraints="до часа")
    state = InterviewState()
    for answer in ("", "развёрнуто", "", "", "евро"):
        state = state.answer(answer)

    profile = state.profile(base, "профиль 1")

    assert profile.style == "развёрнуто"
    assert profile.genres == "евро"
    assert profile.constraints == "до часа"
    assert profile.name == "новичок"


def test_long_answer_is_clipped():
    long_answer = "о" * (config.PROFILE_VALUE_MAX_CHARS + 50)
    state = InterviewState()
    for answer in ("новичок", long_answer, "", "", ""):
        state = state.answer(answer)

    profile = state.profile(UserProfile(), "профиль 1")

    assert len(profile.style) == config.PROFILE_VALUE_MAX_CHARS
    assert profile.style.endswith("…")


# --- сообщение для модели ---


def test_empty_profile_gives_no_message():
    assert profile_message(UserProfile()) is None
    assert profile_message(_profile("новичок")) is None


def test_message_lists_sections_and_the_instruction():
    message = profile_message(
        _profile("новичок", style="коротко и просто", constraints="только игры до часа")
    )

    assert message is not None
    assert "новичок" in message
    assert "Стиль: коротко и просто" in message
    assert "Ограничения: только игры до часа" in message
    assert "\n- Опыт: " not in message
    assert "настройк" in message.lower()
