"""Команда /logictask против stub API: панель выбора, реальные запросы, отрисованный экран."""

import json

from core import logictask

from . import harness
from .stub_api import answer


def _wait_panel_open(session) -> None:
    """Ждём отрисованный кадр панели (хинт виден только внутри Live-панели)."""
    session.wait_on_screen("Enter — решить")


def _select(session, arrows: int) -> None:
    _wait_panel_open(session)
    for _ in range(arrows):
        session.send_key(harness.KEY_DOWN, 1)
    session.send_key(harness.KEY_ENTER, 1)


def _wait_panel_closed(session) -> None:
    """Transient-панель erase'ится при закрытии — ждём исчезновения её заголовка."""
    session.wait_until_gone("Логическая задача")


def test_panel_shown_then_esc_makes_no_requests(app, stub, history_file):
    session = app()
    session.wait_for_prompt()
    session.send_line("/logictask")
    session.wait_on_screen("Выберите стратегию")
    session.wait_on_screen("1. Прямой ответ")
    session.wait_on_screen("4. Панель экспертов")
    session.wait_on_screen("волк")
    session.wait_on_screen("капуст")
    _wait_panel_open(session)

    session.send_key(harness.KEY_ESC, 1)
    _wait_panel_closed(session)
    session.wait_for_prompt()
    assert stub.call_count == 0

    session.send_line("/exit")
    session.wait_exit()
    assert json.loads(history_file.read_text(encoding="utf-8")) == []


def test_chosen_direct_strategy_makes_single_request(app, stub, history_file):
    stub.always(answer("Прямой ответ stub-модели."))
    session = app()
    session.wait_for_prompt()
    session.send_line("/logictask")
    session.wait_on_screen("Выберите стратегию")

    _select(session, arrows=0)  # стратегия 1 уже выбрана

    session.wait_for("Стратегия 1: Прямой ответ")
    session.wait_for("Прямой ответ stub-модели.")
    session.wait_for("Токены: 50+100=150")
    session.wait_for_prompt()
    session.send_line("/exit")
    session.wait_exit()

    assert stub.call_count == 1
    assert logictask.LOGIC_TASK in stub.user_messages()[0]
    assert "только ответ" in stub.user_messages()[0]
    assert stub.system_messages()[0] == logictask.DIRECT_SYSTEM_MESSAGE
    assert json.loads(history_file.read_text(encoding="utf-8")) == []
    assert session.scrollback().count("Токены: 50+100=150") == 1


def test_chosen_strategy3_runs_both_steps(app, stub):
    stub.sequence(
        answer("СОСТАВЛЕННЫЙ ПРОМПТ STUB"),
        answer("Решение по составленному промпту."),
    )
    session = app()
    session.wait_for_prompt()
    session.send_line("/logictask")
    session.wait_on_screen("Выберите стратегию")

    _select(session, arrows=2)  # стратегия 3

    session.wait_for("Стратегия 3: Промпт от модели")
    session.wait_for("СОСТАВЛЕННЫЙ ПРОМПТ STUB")
    session.wait_for_prompt()
    session.send_line("/exit")
    session.wait_exit()

    assert stub.call_count == 2
    assert stub.system_messages()[1] == "СОСТАВЛЕННЫЙ ПРОМПТ STUB"
    assert stub.user_messages()[1] == logictask.LOGIC_TASK
    assert session.scrollback().count("Токены: 50+100=150") == 2


def test_chosen_strategy4_runs_three_experts(app, stub):
    stub.sequence(
        answer("Решение когнитивного психолога."),
        answer("Решение шахматного стратега."),
        answer("Решение специалиста по теории игр."),
    )
    session = app()
    session.wait_for_prompt()
    session.send_line("/logictask")
    session.wait_on_screen("Выберите стратегию")

    for _ in range(3):
        session.send_key(harness.KEY_DOWN, 1)
    session.send_key(harness.KEY_ENTER, 1)

    session.wait_for("Стратегия 4: Панель экспертов")
    session.wait_for("Решение специалиста по теории игр.", timeout=15)
    session.wait_for_prompt()
    session.send_line("/exit")
    session.wait_exit()

    assert stub.call_count == 3
    assert stub.system_messages() == list(logictask.EXPERT_ROLES)
    for user in stub.user_messages():
        assert user == logictask.LOGIC_TASK
    assert session.scrollback().count("Токены: 50+100=150") == 3
