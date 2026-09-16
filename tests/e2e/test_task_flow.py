"""Конвейер задачи в живом приложении: прогон, пауза, перезапуск и изоляция файла задачи."""

import json

import pytest

from . import harness
from .stub_api import Reply, answer

pytestmark = [pytest.mark.e2e, pytest.mark.pty]

PLAN_JSON = '{"items": ["Тема", "Ход игрока", "Подсчёт очков"]}'
SECOND_PLAN_JSON = '{"items": ["Правила", "Компоненты", "Плейтест"]}'
OK_JSON = '{"ok": true, "issues": []}'
FIRST_TASK = "Разработать настольную игру про гонки улиток"
SECOND_TASK = "Собрать подборку игр на вечер для пяти человек"

# Медленные ответы выполнения: панель успевает показать шаг, тест — нажать паузу, а пауза
# срабатывает на границе операции, то есть сразу после того запроса, который был в полёте.
SLOW = 0.6


def _execute(text: str, delay: float = 0.0) -> Reply:
    return Reply(content=text, delay=delay)


def _plan_for_task(stub, task: str) -> dict:
    """Последний запрос планирования по этой задаче — вместе с его user-сообщением."""
    for record in stub.requests:
        payload = record["payload"]
        if task in payload["messages"][-1]["content"] and "Число подзадач" in payload["messages"][-1]["content"]:
            return payload
    raise AssertionError(f"не нашёл запроса планирования для задачи {task!r}")


def _saved(task_file) -> dict:
    return json.loads(task_file.read_text(encoding="utf-8"))


def test_full_run_goes_from_task_to_task_without_a_single_key(app, stub, task_file, tasks_dir):
    """Две задачи подряд: Planning, Execution, Validation, Done — и переход к следующей."""
    stub.sequence(
        answer(PLAN_JSON),
        answer("Раздел 1"),
        answer("Раздел 2"),
        answer("Раздел 3"),
        answer(OK_JSON),
        answer(SECOND_PLAN_JSON),
        answer("Раздел 1"),
        answer("Раздел 2"),
        answer("Раздел 3"),
        answer(OK_JSON),
    )
    with app() as session:
        session.wait_for_prompt()
        session.send_line(f"/task add {FIRST_TASK}")
        session.wait_for("Задача в очереди (1 из 1)")
        session.send_line(f"/task add {SECOND_TASK}")
        session.wait_for("Задача в очереди (2 из 2)")
        session.send_line("/task run")
        session.wait_for("Правки к плану")
        session.send_line("")  # правок нет — план первой задачи принят
        session.wait_for("Итог: ")
        # Вторая задача из очереди: агент сам дошёл до её плана и спрашивает правки снова.
        session.wait_for("[ ] Правила")
        session.send_line("")
        session.wait_for("Очередь задач пуста — прогон остановлен")
        session.send_line("/task")
        session.wait_for("Очередь (2)")

    assert stub.call_count == 10
    assert "stop" not in stub.requests[0]["payload"]
    assert _plan_for_task(stub, FIRST_TASK) is not None
    assert _plan_for_task(stub, SECOND_TASK) is not None
    # Проверка уходит последним запросом первой задачи — с артефактом, а не с планом.
    assert "## 1. Тема" in stub.requests[4]["payload"]["messages"][-1]["content"]

    saved = _saved(task_file)
    assert [task["status"] for task in saved["tasks"]] == ["решена", "решена"]
    results = sorted(path.name for path in tasks_dir.iterdir())
    assert len(results) == 2
    assert results[0].startswith("1-razrabotat-nastolnuyu-igru")
    assert results[1].startswith("2-sobrat-podborku-igr")
    first = (tasks_dir / results[0]).read_text(encoding="utf-8")
    assert first.startswith("# " + FIRST_TASK)
    assert "## 1. Тема" in first
    assert [len(task["sections"]) for task in saved["tasks"]] == [3, 3]
    assert saved["paused"] is False


def test_edits_rebuild_the_plan_before_execution(app, stub, task_file):
    """Непустой ответ на вопрос о правках просит новый круг планирования."""
    stub.sequence(
        _execute(PLAN_JSON, delay=SLOW),
        _execute(SECOND_PLAN_JSON, delay=SLOW),
        answer("Раздел 1"),
        answer("Раздел 2"),
        answer("Раздел 3"),
        answer(OK_JSON),
    )
    with app() as session:
        session.wait_for_prompt()
        session.send_line(f"/task add {FIRST_TASK}")
        session.wait_for("Задача в очереди")
        session.send_line("/task run")
        session.wait_for("Правки к плану")
        # Набираем текст без Enter: панель обязана показывать ввод прямо в себе. Пишем двумя
        # кусками — набранное остаётся на экране, пока не пришёл следующий байт.
        session.send_key("добавь пункт".encode("utf-8"))
        session.wait_for("добавь пункт")
        typing = session.screen_text()
        session.send_key(" про подсчёт очков".encode("utf-8"))
        session.wait_for("про подсчёт очков")
        session.send_key(harness.KEY_ENTER)
        # После нового круга план показывается снова и правки спрашиваются ещё раз:
        # пустая строка принимает план, и выполнение начинается.
        session.wait_for("[ ] Правила", timeout=30)
        session.send_line("")
        session.wait_for("Итог: ")

    assert "Правки к плану (Enter — принять, Ctrl+C — пауза): добавь пункт" in " ".join(
        typing.split()
    )

    plan_requests = [
        record["payload"]
        for record in stub.requests
        if "Число подзадач" in record["payload"]["messages"][-1]["content"]
    ]
    assert len(plan_requests) == 2
    assert "добавь пункт про подсчёт очков" in plan_requests[1]["messages"][-1]["content"]
    assert "1. Тема" in plan_requests[1]["messages"][-1]["content"]

    saved = _saved(task_file)
    assert saved["tasks"][0]["plan_round"] == 2
    assert saved["tasks"][0]["plan"] == ["Правила", "Компоненты", "Плейтест"]


def test_pause_on_the_key_stops_the_run_and_continues_from_the_same_step(app, stub, task_file):
    """Пауза на Execution: строка паузы, состояние в файле, продолжение с того же шага."""
    stub.sequence(
        answer(PLAN_JSON),
        _execute("Раздел 1", delay=SLOW),
        _execute("Раздел 2", delay=SLOW),
        _execute("Раздел 3", delay=SLOW),
        answer(OK_JSON),
    )
    with app() as session:
        session.wait_for_prompt()
        session.send_line(f"/task add {FIRST_TASK}")
        session.wait_for("Задача в очереди")
        session.send_line("/task run")
        session.wait_for("Правки к плану")
        session.send_line("")
        # Панель показывает вторую подзадачу — в этот момент и жмём паузу: она сработает на
        # границе операции, то есть после идущего запроса.
        session.wait_for("2/3", timeout=15)
        session.send_key(b"p")
        session.wait_for("⏸ Пауза")
        session.wait_for_prompt()
        session.send_line("/task")
        session.wait_for("Пауза: да")
        paused_report = session.scrollback()
        session.send_line("/task run")
        session.wait_for("Пауза снята — продолжаю с сохранённого шага")
        session.wait_for("Итог: ")

    assert "Текущий шаг: 3/3: «Подсчёт очков»" in paused_report
    assert "Артефакт: 2 раздела" in paused_report
    assert _saved(task_file)["paused"] is False
    assert _saved(task_file)["tasks"][0]["status"] == "решена"

    # Продолжение не переделывает выполненные подзадачи: следующий запрос — про третью.
    resumed = stub.requests[3]["payload"]["messages"][-1]["content"]
    assert "Твоя подзадача: 3. Подсчёт очков" in resumed
    assert "Тема: Раздел 1" in resumed
    assert "Ход игрока: Раздел 2" in resumed


def test_restart_continues_the_task_from_the_saved_step(app, stub, task_file):
    """Перезапуск приложения: та же задача, тот же шаг, продолжение без объяснений."""
    stub.sequence(
        answer(PLAN_JSON),
        _execute("Раздел 1", delay=SLOW),
        _execute("Раздел 2", delay=SLOW),
        _execute("Раздел 3", delay=SLOW),
        answer(OK_JSON),
    )
    with app() as session:
        session.wait_for_prompt()
        session.send_line(f"/task add {FIRST_TASK}")
        session.wait_for("Задача в очереди")
        session.send_line("/task run")
        session.wait_for("Правки к плану")
        session.send_line("")
        session.wait_for("2/3", timeout=15)
        session.send_key(b"p")
        session.wait_for("⏸ Пауза")
        session.wait_for_prompt()
        session.send_line("/exit")
        session.wait_exit()

    before = stub.call_count
    assert before == 3  # план и две выполненные подзадачи

    with app() as session:
        session.wait_for_prompt()
        session.send_line("/task")
        session.wait_for("Текущая задача")
        restored = session.scrollback()
        session.send_line("/task run")
        session.wait_for("Пауза снята — продолжаю с сохранённого шага")
        session.wait_for("Итог: ")

    assert FIRST_TASK in restored
    assert "Текущий шаг: 3/3: «Подсчёт очков»" in restored
    assert "Пауза: да" in restored
    # Новая сессия не планирует заново: она выполняет третью подзадачу и проверяет артефакт.
    assert stub.call_count == before + 2
    assert "Твоя подзадача: 3. Подсчёт очков" in stub.requests[before]["payload"]["messages"][-1]["content"]


def test_state_message_travels_with_every_question_and_survives_clear(app, stub, task_file):
    """Состояние задачи уходит в обычный вопрос, а /clear очередь не трогает."""
    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.wait_for_prompt()
        session.send_line(f"/task add {FIRST_TASK}")
        session.wait_for("Задача в очереди")
        session.ask("Что делаем на этом шаге?", "Ответ stub-модели.")
        with_state = [m["content"] for m in stub.last_payload()["messages"] if m["role"] == "system"]
        session.send_line("/clear")
        session.wait_for("История диалога очищена")
        session.ask("И что дальше?", "Ответ stub-модели.")
        after_clear = [m["content"] for m in stub.last_payload()["messages"] if m["role"] == "system"]
        session.send_line("/task")
        session.wait_for("Текущая задача")

    assert any("Задача пользователя" in message for message in with_state)
    assert any("Текущий шаг" in message for message in with_state)
    assert any("Задача пользователя" in message for message in after_clear)
    assert FIRST_TASK in session.scrollback()
    assert _saved(task_file)["tasks"][0]["goal"] == FIRST_TASK


def test_report_on_the_empty_queue_and_failed_task_keep_the_run_alive(app, stub, task_file):
    """Пустая очередь даёт подсказку, а недоступная модель помечает задачу не удавшейся."""
    from .stub_api import failure

    stub.sequence(failure(500), failure(500), failure(500), failure(500))
    with app() as session:
        session.wait_for_prompt()
        session.send_line("/task")
        session.wait_for("Очередь задач пуста")
        session.send_line(f"/task add {FIRST_TASK}")
        session.wait_for("Задача в очереди")
        session.send_line("/task run")
        session.wait_for("Итог: ")
        session.wait_for("Очередь задач пуста — прогон остановлен")
        session.send_line("/task")
        session.wait_for("не удалось")

    assert _saved(task_file)["tasks"][0]["status"] == "не удалось"
    assert _saved(task_file)["tasks"][0]["error"]


def test_real_task_file_of_the_repository_is_not_touched(app, stub, task_file, tasks_dir):
    repo_task = harness.REPO_ROOT / "task.json"
    before = repo_task.stat().st_mtime_ns if repo_task.exists() else None
    repo_results = harness.REPO_ROOT / "tasks"
    before_results = sorted(path.name for path in repo_results.iterdir()) if repo_results.exists() else []

    stub.always(answer("Ответ stub-модели."))
    with app() as session:
        session.wait_for_prompt()
        session.send_line(f"/task add {FIRST_TASK}")
        session.wait_for("Задача в очереди")
        session.send_line("/exit")
        session.wait_exit()

    after = repo_task.stat().st_mtime_ns if repo_task.exists() else None
    after_results = sorted(path.name for path in repo_results.iterdir()) if repo_results.exists() else []
    assert before == after
    assert before_results == after_results
    assert _saved(task_file)["tasks"][0]["goal"] == FIRST_TASK


NEW_ITEM_VERDICT = (
    '{"ok": false, "issues": [{"item": 0, "issue": "в артефакте нет подсчёта очков"}],'
    ' "items": ["Описать подсчёт очков и конец партии"]}'
)


def test_panel_stays_on_screen_while_the_user_answers(app, stub, task_file):
    """Панель видна и во время вопроса: состояние задачи не исчезает, пока ждут ответа."""
    stub.sequence(
        _execute(PLAN_JSON, delay=SLOW),
        _execute(SECOND_PLAN_JSON, delay=SLOW),
        answer("Раздел 1"),
        answer("Раздел 2"),
        answer("Раздел 3"),
        answer(OK_JSON),
    )
    with app() as session:
        session.wait_for_prompt()
        session.send_line(f"/task add {FIRST_TASK}")
        session.wait_for("Задача в очереди")
        session.send_line("/task run")
        session.wait_for("Правки к плану")
        while_asking = session.screen_text()

        assert "Задача 1/1" in while_asking  # панель
        assert "Правки к плану (Enter — принять, Ctrl+C — пауза)" in while_asking
        assert "> Введите вопрос (или /exit для выхода):" in while_asking  # поле ввода
        collapsed = " ".join(while_asking.split())
        assert "Последний запрос: Planning" in collapsed  # траты с пометкой запроса
        assert "За прогон: 1 запрос" in collapsed
        assert "Задача: 1/1" in while_asking  # статус-бар под панелью, не скрыт
        assert "[ ] Тема" in while_asking  # план списком, по подзадаче на строку

        session.send_line("добавь пункт про подсчёт очков")
        session.wait_for("[ ] Правила")  # второй круг планирования
        session.send_line("")
        session.wait_for("Итог: ")


def test_validation_subtask_appears_in_the_panel_and_is_executed(app, stub, task_file):
    """Новая подзадача от проверки попадает в план, показывается в панели и выполняется."""
    stub.sequence(
        answer(PLAN_JSON),
        answer("Раздел 1"),
        answer("Раздел 2"),
        answer("Раздел 3"),
        answer(NEW_ITEM_VERDICT),
        _execute("Очки: по одному за улитку на финише.", delay=SLOW),
        _execute('{"ok": true, "issues": []}', delay=SLOW),
    )
    with app() as session:
        session.wait_for_prompt()
        session.send_line(f"/task add {FIRST_TASK}")
        session.wait_for("Задача в очереди")
        session.send_line("/task run")
        session.wait_for("Правки к плану")
        session.send_line(" ")
        session.wait_for("Описать подсчёт очков и конец партии")  # новая подзадача в панели
        session.wait_for("Итог: ")
        session.send_line("/task")
        session.wait_for("Незавершённых задач нет")

    saved = _saved(task_file)
    assert saved["tasks"][0]["plan"][-1] == "Описать подсчёт очков и конец партии"
    assert len(saved["tasks"][0]["sections"]) == 4
    assert saved["tasks"][0]["status"] == "решена"


def test_typing_during_the_run_pauses_and_the_main_loop_runs_the_line(app, stub, task_file):
    """Набранная во время прогона строка видна в поле ввода, а по Enter прогон встаёт на паузу."""
    stub.sequence(
        answer(PLAN_JSON),
        _execute("Раздел 1", delay=SLOW),
        _execute("Раздел 2", delay=SLOW),
        _execute("Раздел 3", delay=SLOW),
        answer(OK_JSON),
    )
    with app() as session:
        session.wait_for_prompt()
        session.send_line(f"/task add {FIRST_TASK}")
        session.wait_for("Задача в очереди")
        session.send_line("/task run")
        session.wait_for("Правки к плану")
        session.send_line(" ")
        session.wait_for("▸ Execution")
        session.wait_for("Текущий шаг:")

        session.send_key("/task".encode("utf-8"))
        session.wait_for("> Введите вопрос (или /exit для выхода): /task")
        session.send_key(harness.KEY_ENTER)
        session.wait_for("⏸ Пауза")
        session.wait_for("Очередь (1)")  # строку обработал главный цикл
        session.send_line("/task run")
        session.wait_for("Пауза снята")
        session.wait_for("Итог: ")

    saved = _saved(task_file)
    assert saved["tasks"][0]["status"] == "решена"
    assert len(saved["tasks"][0]["sections"]) == 3


def test_fix_pass_does_not_redo_ready_sections(app, stub, task_file):
    """Замечание о готовых разделах не запускает их заново: новых запросов на них нет."""
    issues_json = (
        '{"ok": false, "issues": [{"item": 1, "issue": "тема раскрыта слабо"}],'
        ' "items": ["Дописать условия победы"]}'
    )
    stub.sequence(
        answer(PLAN_JSON),
        answer("Раздел 1"),
        answer("Раздел 2"),
        answer("Раздел 3"),
        answer(issues_json),
        _execute("Условия победы: первый на финише.", delay=SLOW),
        answer('{"ok": true, "issues": []}'),
    )
    with app() as session:
        session.wait_for_prompt()
        session.send_line(f"/task add {FIRST_TASK}")
        session.wait_for("Задача в очереди")
        session.send_line("/task run")
        session.wait_for("Правки к плану")
        session.send_line(" ")
        session.wait_for("Итог: ")

    executed = [
        record["payload"]["messages"][-1]["content"]
        for record in stub.requests
        if "Твоя подзадача" in record["payload"]["messages"][-1]["content"]
    ]
    # Выполнены три подзадачи плана и одна новая от проверки; готовые разделы не переделывались.
    assert len(executed) == 4
    assert "Дописать условия победы" in executed[-1]

    saved = _saved(task_file)
    assert saved["tasks"][0]["plan"][-1] == "Дописать условия победы"
    assert len(saved["tasks"][0]["sections"]) == 4


def test_metrics_lines_are_not_printed_during_a_task_run(app, stub, task_file):
    """Во время прогона журнал чист: траты видны в панели, а строк «⏱» в нём нет."""
    stub.sequence(
        _execute(PLAN_JSON, delay=SLOW),
        answer("Раздел 1"),
        answer("Раздел 2"),
        answer("Раздел 3"),
        _execute(OK_JSON, delay=SLOW),
    )
    with app() as session:
        session.wait_for_prompt()
        session.send_line(f"/task add {FIRST_TASK}")
        session.wait_for("Задача в очереди")
        session.send_line("/task run")
        session.wait_for("Правки к плану")
        in_run = session.scrollback()
        assert "За прогон:" in " ".join(in_run.split())  # траты — в панели
        session.send_line(" ")
        session.wait_for("Итог: ")
        after = session.scrollback()

    assert "⏱" not in after
