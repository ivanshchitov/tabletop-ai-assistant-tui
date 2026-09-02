"""Снапшоты отрисованного экрана.

Точечные ассерты проверяют присутствие фраз, но не замечают, что рамка съехала, статус-бар
перенёсся иначе или пропал разделитель. Здесь экран фиксируется целиком; обновлять снапшоты
после осознанного изменения вёрстки — `pytest --snapshot-update`.
"""

from pathlib import Path

import pytest

from .harness import KEY_DOWN, KEY_RIGHT, assert_snapshot
from .stub_api import answer, failure

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

SNAPSHOTS = Path(__file__).parent / "snapshots"


def test_startup_screen_snapshot(app, snapshot_update):
    with app() as session:
        session.wait_for_prompt()
        session.read_for(0.3)
        assert_snapshot(session.screen_text(), SNAPSHOTS / "startup.txt", snapshot_update)


def test_answer_screen_snapshot(app, stub, snapshot_update):
    stub.always(
        answer(
            "# Каркассон\n\n"
            "- **Жанр:** размещение тайлов\n"
            "- **Игроков:** 2–5\n\n"
            "Итог: начните с базовой коробки. 🎲"
        )
    )
    with app() as session:
        session.ask("Расскажи про Каркассон", "начните с базовой коробки")
        session.read_for(0.3)
        assert_snapshot(session.screen_text(), SNAPSHOTS / "answer.txt", snapshot_update)


def test_settings_panel_snapshot(app, snapshot_update):
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/settings")
        session.wait_on_screen("Лимит вариантов в списке")
        session.send_keys(KEY_RIGHT)
        session.send_keys(KEY_DOWN)
        session.read_for(0.4)
        assert_snapshot(session.screen_text(), SNAPSHOTS / "settings_panel.txt", snapshot_update)


def test_error_screen_snapshot(app, stub, snapshot_update):
    stub.always(failure(500))
    with app() as session:
        session.ask("Вопрос", "Попробуйте повторить запрос")
        session.read_for(0.3)
        assert_snapshot(session.screen_text(), SNAPSHOTS / "api_error.txt", snapshot_update)
