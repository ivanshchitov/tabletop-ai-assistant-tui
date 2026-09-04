"""Доп.-информация (время/токены/стоимость) после ответа модели.

Stub-сервер отдаёт фиксированный usage (см. stub_api.Reply.body): prompt_tokens=50,
completion_tokens=100 — стоимость для deepseek-v4-flash (0.22$/0.66$ за 1M) считается как
(50*0.22 + 100*0.66) / 1_000_000 = 0.000077.
"""

import json


def test_question_answer_shows_usage_metadata(app, stub):
    session = app()
    session.wait_for_prompt()
    session.send_line("Какие правила у игры Каркассон?")
    session.wait_for("Ответ stub-сервера.")

    session.wait_for("Токены: 50+100=150")
    session.wait_for("$0.000077")

    session.send_line("/exit")
    session.wait_exit()


def test_usage_metadata_not_saved_to_history(app, stub, history_file):
    session = app()
    session.wait_for_prompt()
    session.send_line("Какие правила у игры Каркассон?")
    session.wait_for("Ответ stub-сервера.")
    session.wait_for("Токены: 50+100=150")

    session.send_line("/exit")
    session.wait_exit()

    saved = json.loads(history_file.read_text(encoding="utf-8"))
    assert saved[0]["question"] == "Какие правила у игры Каркассон?"
    assert saved[0]["answer"] == "Ответ stub-сервера."
    assert "Токены" not in json.dumps(saved, ensure_ascii=False)
