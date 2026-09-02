"""Конструкторы промптов для команды /logictask (четыре стратегии)."""

from core import logictask, prompts
from core.answer_settings import AnswerFormat, AnswerSettings


def test_fixed_task_constant_mentions_all_cargo_and_minimum_crossings():
    """Фиксированная задача: волк, коза, капуста и требование минимального числа рейсов."""
    task = logictask.LOGIC_TASK
    assert "волк" in task
    assert "коз" in task
    assert "капуст" in task
    assert "минимальн" in task


# --- стратегия 1: прямой ответ ---------------------------------------------------------


def test_direct_strategy_sends_task_without_any_instructions():
    """Прямой запрос: системное сообщение нейтрально, в user — задача и условие «только ответ»."""
    system, user = logictask.build_direct_prompts()
    assert system.strip() == logictask.DIRECT_SYSTEM_MESSAGE
    assert logictask.LOGIC_TASK in user


def test_direct_strategy_forces_answer_only():
    """Условие «только ответ»: без решения, без шагов, без выводов."""
    _, user = logictask.build_direct_prompts()
    assert "только ответ" in user
    assert "без решения" in user
    assert "без шагов" in user
    assert "без выводов" in user


def test_direct_strategy_system_message_is_neutral():
    """Системное сообщение не тянет базовый промпт и форматные инструкции."""
    system, _ = logictask.build_direct_prompts()
    base = prompts._get_base_system_prompt()
    assert base not in system
    for fmt in AnswerFormat:
        instruction = prompts.get_format_instruction(fmt)
        if instruction:
            assert instruction not in system


def test_direct_strategy_ignores_active_settings():
    """Активные настройки ответа не влияют на промпт прямой стратегии."""
    settings = AnswerSettings().with_format(AnswerFormat.JSON).with_max_words(30)
    prompts.build_user_prompt("Что угодно", settings)  # фиксирует инструкции настроек
    system, user = logictask.build_direct_prompts()
    assert "JSON" not in system + user
    assert "слов" not in system + user
    assert "вариант" not in system + user


# --- стратегия 2: пошаговое решение -----------------------------------------------------


def test_stepwise_strategy_contains_task_and_step_instruction():
    """Пошаговый запрос: текст задачи + инструкция решать пошагово."""
    system, user = logictask.build_stepwise_prompts()
    assert logictask.LOGIC_TASK in user
    assert "пошагово" in user.lower() or "по шагам" in user.lower()


def test_stepwise_strategy_has_no_role_or_expert_additions():
    """Пошаговый промпт не содержит ролей, экспертов и генерации промпта."""
    _, user = logictask.build_stepwise_prompts()
    for word in ("эксперт", "роль", "составь промпт", "напиши промпт"):
        assert word not in user.lower()


def test_stepwise_system_message_differs_from_direct():
    """Стратегии 1 и 2 различаются — иначе сравнение стратегий бессмысленно."""
    direct = logictask.build_direct_prompts()
    stepwise = logictask.build_stepwise_prompts()
    assert stepwise != direct


# --- стратегия 3: промпт от модели ------------------------------------------------------


def test_prompt_from_model_first_request_asks_to_compose_prompt():
    """Первый запрос: текст задачи + просьба составить промпт для её решения."""
    system, user = logictask.build_prompt_compose_prompts()
    assert logictask.LOGIC_TASK in user
    assert "промпт" in user.lower()
    assert "состав" in user.lower() or "напиши" in user.lower()


def test_prompt_from_model_second_request_carries_composed_prompt():
    """Второй запрос: составленный моделью промпт идёт в system, задача — в user."""
    composed = "Составленный моделью промпт дословно."
    system, user = logictask.build_solve_with_prompt_prompts(composed)
    assert system == composed
    assert logictask.LOGIC_TASK == user


def test_prompt_from_model_second_request_keeps_composed_prompt_untouched():
    """Промпт модели передаётся без ручной правки (включая пустые строки по краям)."""
    composed = "  промпт с пробелами  "
    system, _ = logictask.build_solve_with_prompt_prompts(composed)
    assert system == composed


# --- стратегия 4: панель экспертов ------------------------------------------------------


def test_expert_panel_defines_exactly_three_experts():
    """Трое экспертов зафиксированы: повар, психолог животных, теоретик игр."""
    assert len(logictask.EXPERT_ROLES) == 3
    joined = " ".join(logictask.EXPERT_ROLES).lower()
    assert "повар" in joined
    assert "психолог" in joined
    assert "живот" in joined
    assert "между собой" in joined and "с людьми" in joined
    assert "теори" in joined and "игр" in joined
    assert "психол" in joined and "психолог" in joined
    assert "шахмат" not in joined


def test_expert_panel_builds_one_request_per_expert():
    """На каждого эксперта — свой запрос: роль в system, задача в user."""
    prompts_per_expert = [
        logictask.build_expert_prompts(role) for role in logictask.EXPERT_ROLES
    ]
    systems = [system for system, _ in prompts_per_expert]
    assert systems == list(logictask.EXPERT_ROLES)
    for _, user in prompts_per_expert:
        assert user == logictask.LOGIC_TASK


def test_expert_roles_are_not_asked_from_the_model():
    """Роли экспертов — константы, а не результат запроса к модели."""
    assert isinstance(logictask.EXPERT_ROLES, tuple)
    assert all(isinstance(role, str) and role.strip() for role in logictask.EXPERT_ROLES)


# --- заголовки стратегий ----------------------------------------------------------------


def test_strategy_headers_numbered_and_named():
    """Четыре заголовка: номер + название стратегии."""
    headers = logictask.STRATEGY_HEADERS
    assert len(headers) == 4
    for number, (index, title) in enumerate(headers, start=1):
        assert index == number
        assert title.strip()


def test_strategy_headers_cover_all_four_techniques():
    """Названия отражают четыре техники: прямой, пошагово, промпт от модели, эксперты."""
    joined = " ".join(title for _, title in logictask.STRATEGY_HEADERS).lower()
    assert "прямо" in joined
    assert "пошагов" in joined or "по шагам" in joined
    assert "промпт" in joined
    assert "эксперт" in joined
