"""Состояние задачи: очередь, переходы автомата, пауза, хранилище и сообщение для модели."""

import importlib
import json

import pytest

from core import config, task_state
from core.task_state import Stage, TaskState, TaskStatus, TaskStore

PLAN = ("Тема и жанр", "Ход игрока", "Подсчёт очков")


def test_task_file_paths_and_limits(monkeypatch, tmp_path):
    """Путь и границы — часть конфигурации: без переключателя прогон писал бы реальный файл."""
    assert config.TASK_FILE == config.BASE_DIR / "task.json"
    assert config.TASK_RESULTS_DIR == config.BASE_DIR / "tasks"
    assert config.MAX_VALIDATION_ATTEMPTS >= 2
    assert 50 <= config.TASK_GOAL_MAX_CHARS <= 1000
    assert 1 <= config.MIN_PLAN_ITEMS <= config.MAX_PLAN_ITEMS <= 10
    assert 1 <= config.MAX_PLAN_ROUNDS <= 10
    assert 1 <= config.MAX_VALIDATION_ATTEMPTS <= 5
    assert 50 <= config.TASK_PLAN_MAX_WORDS <= 1000
    assert 50 <= config.TASK_SECTION_MAX_WORDS <= 1000
    assert 50 <= config.TASK_VALIDATE_MAX_WORDS <= 1000

    monkeypatch.setenv("TABLETOP_TASK_FILE", str(tmp_path / "other-task.json"))
    monkeypatch.setenv("TABLETOP_TASKS_DIR", str(tmp_path / "other-tasks"))
    try:
        reloaded = importlib.reload(config)
        assert reloaded.TASK_FILE == tmp_path / "other-task.json"
        assert reloaded.TASK_RESULTS_DIR == tmp_path / "other-tasks"
    finally:
        monkeypatch.delenv("TABLETOP_TASK_FILE", raising=False)
        monkeypatch.delenv("TABLETOP_TASKS_DIR", raising=False)
        importlib.reload(config)


# --- заготовки ---


def _state(goal: str = "Разработать игру про улиток") -> TaskState:
    return task_state.add_task(TaskState(), goal)


def _planned(state: TaskState = None, plan=PLAN) -> TaskState:
    return task_state.plan_built(task_state.begin_task(state or _state()), plan)


def _executing(state: TaskState = None, plan=PLAN, sections: int = 1) -> TaskState:
    state = task_state.accept_plan(_planned(state, plan))
    for index in range(sections):
        state = task_state.add_section(state, f"Раздел {index + 1}")
    return state


def _done(state: TaskState = None, plan=PLAN) -> TaskState:
    """Задача, доведённая до Done: проверка без замечаний завершает её."""
    return task_state.validation_verdict(
        task_state.start_validation(_executing(state, plan, sections=len(plan))), []
    )


# --- поля состояния: этап, текущий шаг, ожидаемое действие ---


def test_new_task_is_pending_in_planning():
    state = _state()

    assert len(state.tasks) == 1
    assert state.tasks[0].status is TaskStatus.PENDING
    assert state.tasks[0].stage is Stage.PLANNING
    assert state.active_index == 0
    assert state.current_step == "круг 1: план не построен"
    assert state.expected_action == "построить план задачи"


def test_queue_keeps_order_and_active_is_the_first_unfinished():
    state = _done(task_state.add_task(_state(), "Вторая задача"))

    assert [task.goal for task in state.tasks] == [
        "Разработать игру про улиток",
        "Вторая задача",
    ]
    assert state.tasks[0].status is TaskStatus.DONE
    assert state.active_index == 1
    assert state.finished is False
    assert [task.goal for task in state.tasks[1:]] == ["Вторая задача"]


def test_goal_is_clipped_to_the_limit():
    state = task_state.add_task(TaskState(), "ц" * (config.TASK_GOAL_MAX_CHARS + 50))

    assert len(state.tasks[0].goal) == config.TASK_GOAL_MAX_CHARS


def test_empty_goal_does_not_create_a_task():
    assert task_state.add_task(TaskState(), "   ").tasks == ()


def test_step_and_action_while_waiting_for_edits():
    state = _planned()

    assert state.awaiting_edits is True
    assert state.current_step == "круг 1, подзадач 3 — жду правок"
    assert state.expected_action == "дождаться правок пользователя к плану"


def test_step_and_action_on_execution():
    state = _executing(sections=1)

    assert state.current_step == "2/3: «Ход игрока»"
    assert state.expected_action == "выполнить подзадачу «Ход игрока»"


def test_step_and_action_on_execution_fix_pass():
    """Круг исправления выполняет подзадачу, которую назвала проверка."""
    state = task_state.validation_verdict(
        task_state.start_validation(_executing(sections=3)),
        [task_state.TaskIssue(item=0, text="в артефакте нет подсчёта очков")],
        ("Описать подсчёт очков",),
    )

    assert state.tasks[0].stage is Stage.EXECUTION
    assert state.current_step == "4/4: «Описать подсчёт очков»"
    assert state.expected_action == "исправить по замечаниям: «Описать подсчёт очков»"


def test_step_and_action_on_validation():
    state = task_state.start_validation(_executing(sections=3))

    assert state.current_step == f"попытка 1/{config.MAX_VALIDATION_ATTEMPTS}"
    assert state.expected_action == "сверить артефакт с планом и получить вердикт"


def test_step_and_action_when_the_queue_is_exhausted():
    state = _done()

    assert state.finished is True
    assert state.active_index == -1
    assert state.expected_action == "очередь пуста — прогон остановлен"


def test_step_and_action_without_any_task():
    assert task_state.TaskState().current_step == "задач нет"


# --- переходы ---


def test_plan_built_waits_for_edits_and_counts_the_round():
    state = _planned()

    assert state.tasks[0].plan == PLAN
    assert state.tasks[0].plan_round == 1
    assert state.tasks[0].status is TaskStatus.RUNNING
    assert state.tasks[0].stage is Stage.PLANNING
    assert state.awaiting_edits is True


def test_edits_ask_for_a_new_plan_round():
    state = task_state.edits_response(_planned(), "добавь пункт про подсчёт очков")

    assert state.awaiting_edits is False
    assert state.tasks[0].replan is True
    assert state.tasks[0].plan_round == 1

    rebuilt = task_state.plan_built(state, ("Новый план",))
    assert rebuilt.tasks[0].plan_round == 2
    assert rebuilt.tasks[0].plan == ("Новый план",)
    assert rebuilt.tasks[0].replan is False
    assert rebuilt.awaiting_edits is True


def test_empty_edits_finish_planning():
    state = task_state.edits_response(_planned(), "   ")

    assert state.awaiting_edits is False
    assert state.tasks[0].replan is False
    assert state.tasks[0].stage is Stage.EXECUTION


def test_plan_rounds_are_bounded_and_the_plan_is_taken_as_is():
    state = _planned()
    for round_number in range(2, config.MAX_PLAN_ROUNDS + 1):
        state = task_state.edits_response(state, "ещё правка")
        state = task_state.plan_built(state, (f"План круга {round_number}",))
    assert state.tasks[0].plan_round == config.MAX_PLAN_ROUNDS

    state = task_state.edits_response(state, "ещё правка")

    assert state.tasks[0].replan is False
    assert state.tasks[0].stage is Stage.EXECUTION
    assert state.tasks[0].plan == (f"План круга {config.MAX_PLAN_ROUNDS}",)


def test_plan_is_clipped_to_the_number_of_items():
    state = task_state.plan_built(
        task_state.begin_task(_state()), tuple(f"Подзадача {n}" for n in range(1, 20))
    )

    assert len(state.tasks[0].plan) == config.MAX_PLAN_ITEMS


def test_execution_writes_sections_in_plan_order():
    state = _executing(sections=2)

    assert [section.item for section in state.tasks[0].sections] == [
        "Тема и жанр",
        "Ход игрока",
    ]
    assert task_state.next_item_index(state.tasks[0]) == 2


def test_all_sections_done_leave_no_item_to_run():
    assert task_state.next_item_index(_executing(sections=3).tasks[0]) is None


def test_validation_without_issues_finishes_the_task():
    state = task_state.validation_verdict(task_state.start_validation(_executing(sections=3)), [])

    assert state.tasks[0].stage is Stage.DONE
    assert state.tasks[0].status is TaskStatus.DONE
    assert state.tasks[0].issues == ()
    assert state.finished is True


def test_validation_issues_send_the_task_back_to_execution():
    """Незакрытая работа возвращает задачу в Execution; готовые разделы не переделываются."""
    state = task_state.validation_verdict(
        task_state.start_validation(_executing(sections=2)),
        [task_state.TaskIssue(item=3, text="нет подсчёта очков")],
    )

    assert state.tasks[0].stage is Stage.EXECUTION
    assert state.tasks[0].fixing == (2,)
    assert state.tasks[0].attempt == 2
    assert task_state.next_item_index(state.tasks[0]) == 2
    assert "нет подсчёта очков" in state.tasks[0].issues[0].text


def test_issues_about_ready_sections_do_not_restart_them():
    """Замечание к готовому разделу не запускает его заново — сделанное не переделывают."""
    state = task_state.validation_verdict(
        task_state.start_validation(_executing(sections=3)),
        [task_state.TaskIssue(item=2, text="ход игрока описан слабо")],
    )

    assert state.tasks[0].stage is Stage.DONE
    assert state.tasks[0].fixing == ()
    assert [issue.text for issue in state.tasks[0].issues] == ["ход игрока описан слабо"]


def test_attempts_are_bounded_and_done_keeps_unresolved_issues():
    issues = [task_state.TaskIssue(item=2, text="ход игрока описан неполно")]
    state = task_state.start_validation(_executing(sections=3))
    for _ in range(config.MAX_VALIDATION_ATTEMPTS):
        state = task_state.validation_verdict(state, issues, ("Дописать ход игрока",))

    assert state.tasks[0].stage is Stage.DONE
    assert state.tasks[0].status is TaskStatus.DONE
    assert state.tasks[0].attempt == config.MAX_VALIDATION_ATTEMPTS
    assert [issue.text for issue in state.tasks[0].issues] == ["ход игрока описан неполно"]
    assert state.finished is True


def test_issue_without_a_target_does_not_loop_forever():
    """Общее замечание без номера подзадачи: исправлять нечего, задача завершается с ним."""
    state = task_state.validation_verdict(
        task_state.start_validation(_executing(sections=3)),
        [task_state.TaskIssue(item=0, text="артефакт выглядит слабо")],
    )

    assert state.tasks[0].fixing == ()
    assert state.tasks[0].stage is Stage.DONE
    assert [issue.text for issue in state.tasks[0].issues] == ["артефакт выглядит слабо"]


def test_failed_task_keeps_the_queue_moving():
    state = task_state.fail_task(_planned(task_state.add_task(_state(), "Вторая")), "API недоступен")

    assert state.tasks[0].status is TaskStatus.FAILED
    assert state.tasks[0].error == "API недоступен"
    assert state.awaiting_edits is False
    assert state.active_index == 1
    assert state.tasks[1].stage is Stage.PLANNING


def test_failed_section_is_marked_and_keeps_the_plan_slot():
    state = task_state.add_section(_executing(sections=1), "", failed="ответ обрезан")

    section = state.tasks[0].sections[1]
    assert section.failed == "ответ обрезан"
    assert section.text == ""
    assert task_state.next_item_index(state.tasks[0]) == 2


def test_failed_section_is_executed_again_in_place():
    """Провалившийся раздел — незакрытая работа: в круге исправления его выполняют заново."""
    state = task_state.add_section(_executing(sections=2), "", failed="таймаут")
    state = task_state.validation_verdict(
        task_state.start_validation(state),
        [task_state.TaskIssue(item=3, text="подзадача не выполнена: таймаут")],
    )

    assert state.tasks[0].stage is Stage.EXECUTION
    assert state.tasks[0].fixing == (2,)

    state = task_state.add_section(state, "Подсчёт: по одному очку за улитку.")

    assert len(state.tasks[0].sections) == 3
    assert state.tasks[0].sections[2].text == "Подсчёт: по одному очку за улитку."
    assert state.tasks[0].fixing == ()


# --- пауза ---


def _states_at_every_stage():
    two_tasks = lambda: task_state.add_task(_state(), "Вторая задача")  # noqa: E731
    return [
        ("Planning без плана", _state()),
        ("Planning в ожидании правок", _planned()),
        ("Planning после правок", task_state.edits_response(_planned(), "правка")),
        ("Execution", _executing(sections=1)),
        ("Validation", task_state.start_validation(_executing(sections=3))),
        ("Done, очередь не пуста", _done(two_tasks())),
    ]


@pytest.mark.parametrize("name,state", _states_at_every_stage())
def test_pause_and_resume_keep_the_same_step(name, state):
    paused = task_state.set_paused(state, True)

    assert paused.paused is True
    assert paused.current_step == state.current_step
    assert paused.expected_action == state.expected_action
    assert paused.tasks[0].stage is state.tasks[0].stage

    resumed = task_state.set_paused(paused, False)
    assert resumed.paused is False
    assert resumed.current_step == state.current_step


def test_pause_is_not_a_stage_change():
    state = task_state.set_paused(_executing(sections=1), True)

    assert state.tasks[0].stage is Stage.EXECUTION
    assert state.tasks[0].status is TaskStatus.RUNNING


# --- хранилище ---


def _store(tmp_path) -> TaskStore:
    return TaskStore(tmp_path / "task.json")


def test_missing_file_reads_as_an_empty_queue(tmp_path):
    store = _store(tmp_path)

    assert store.state.tasks == ()
    assert store.state.finished is True


def test_broken_json_reads_as_an_empty_queue(tmp_path):
    (tmp_path / "task.json").write_text("{не json", encoding="utf-8")

    assert _store(tmp_path).state.tasks == ()


def test_foreign_shape_reads_as_an_empty_queue(tmp_path):
    (tmp_path / "task.json").write_text('["просто", "список"]', encoding="utf-8")

    assert _store(tmp_path).state.tasks == ()


def test_partially_filled_task_reads_with_defaults(tmp_path):
    (tmp_path / "task.json").write_text(
        json.dumps({"tasks": [{"goal": "Задача"}]}), encoding="utf-8"
    )

    task = _store(tmp_path).state.tasks[0]
    assert task.goal == "Задача"
    assert task.stage is Stage.PLANNING
    assert task.status is TaskStatus.PENDING
    assert task.plan == ()
    assert task.sections == ()


def test_state_round_trips_through_the_file(tmp_path):
    store = _store(tmp_path)
    state = task_state.set_paused(_executing(sections=2), True)
    store.save(state)

    restored = _store(tmp_path).state

    assert restored.paused is True
    assert restored.awaiting_edits is False
    assert restored.tasks[0].goal == state.tasks[0].goal
    assert restored.tasks[0].stage is Stage.EXECUTION
    assert restored.tasks[0].plan == state.tasks[0].plan
    assert restored.tasks[0].plan_limit == state.tasks[0].plan_limit
    assert [section.text for section in restored.tasks[0].sections] == ["Раздел 1", "Раздел 2"]
    assert restored.current_step == state.current_step


def test_verdict_fields_survive_the_file(tmp_path):
    store = _store(tmp_path)
    state = task_state.validation_verdict(
        task_state.start_validation(_executing(sections=3)),
        [task_state.TaskIssue(item=1, text="тема не раскрыта")],
        ("Описать подсчёт очков",),
    )
    store.save(state)

    restored = _store(tmp_path).state.tasks[0]

    assert restored.fixing == (3,)
    assert restored.attempt == 2
    assert restored.issues[0].text == "тема не раскрыта"
    assert restored.issues[0].item == 1


def test_save_leaves_no_temporary_file_behind(tmp_path):
    store = _store(tmp_path)
    store.save(_planned())

    assert [path.name for path in tmp_path.iterdir()] == ["task.json"]
    assert json.loads((tmp_path / "task.json").read_text(encoding="utf-8"))["version"] == 1


def test_write_error_does_not_break_the_state(tmp_path):
    store = _store(tmp_path / "нет-такого-каталога" / "task.json")
    store.save(_planned())

    assert store.state.tasks[0].plan_round == 1


# --- сообщение состояния ---


def test_message_is_empty_without_a_task():
    assert task_state.task_message(TaskState()) is None
    assert task_state.task_message(_done()) is None


def test_message_carries_the_goal_stage_step_and_action():
    asset = (config.ASSETS_DIR / "task_prompt.md").read_text(encoding="utf-8").strip()
    message = task_state.task_message(_executing(sections=1))

    assert "улиток" in message
    assert "Execution" in message
    assert "2/3" in message
    assert "Ход игрока" in message
    assert asset in message


def test_message_of_a_paused_task_says_so():
    message = task_state.task_message(task_state.set_paused(_executing(sections=1), True))

    assert "на паузе" in message.lower()


def test_validation_can_add_a_subtask_to_the_plan():
    """Проверка назвала работу, которой в плане не было: она становится подзадачей."""
    state = task_state.start_validation(_executing(sections=3))

    state = task_state.validation_verdict(
        state,
        [task_state.TaskIssue(item=0, text="в правилах нет подсчёта очков")],
        ("Описать подсчёт очков и конец партии",),
    )

    assert state.tasks[0].stage is Stage.EXECUTION
    assert state.tasks[0].plan[-1] == "Описать подсчёт очков и конец партии"
    assert state.tasks[0].fixing == (3,)
    assert task_state.next_item_index(state.tasks[0]) == 3

    state = task_state.add_section(state, "Подсчёт: по очку за улитку на финише.")
    assert len(state.tasks[0].sections) == 4
    assert state.tasks[0].fixing == ()

    state = task_state.validation_verdict(task_state.start_validation(state), [])
    assert state.tasks[0].status is TaskStatus.DONE
    assert state.tasks[0].plan[-1] == "Описать подсчёт очков и конец партии"


def test_first_plan_is_capped_by_the_ceiling():
    """Первый план не длиннее потолка: слишком длинный план растянул бы прогон."""
    state = task_state.plan_built(
        task_state.begin_task(_state()), tuple(f"Подзадача {n}" for n in range(1, 30))
    )

    assert len(state.tasks[0].plan) == config.MAX_PLAN_ITEMS
    assert state.tasks[0].plan_limit == config.MAX_PLAN_ITEMS


def test_user_edits_raise_the_plan_ceiling():
    """Правки пользователя — не повод терять подзадачи: потолок поднимается под план."""
    state = task_state.edits_response(_planned(), "добавь ещё пять пунктов")
    long_plan = tuple(f"Пункт {n}" for n in range(config.MAX_PLAN_ITEMS + 2))

    state = task_state.plan_built(state, long_plan)

    assert state.tasks[0].plan == long_plan
    assert state.tasks[0].plan_limit == len(long_plan)


def test_new_subtasks_raise_the_plan_ceiling():
    """Подзадачи от проверки тоже не режутся: потолок растёт вместе с планом."""
    state = task_state.start_validation(_executing(sections=3))
    additions = tuple(f"Дописать {n}" for n in range(config.MAX_PLAN_ITEMS + 3))

    state = task_state.validation_verdict(
        state, [task_state.TaskIssue(item=1, text="мало")], additions
    )

    assert len(state.tasks[0].plan) == 3 + len(additions)
    assert state.tasks[0].plan_limit == 3 + len(additions)
    assert state.tasks[0].fixing == tuple(range(3, 3 + len(additions)))


def test_new_subtasks_without_a_fix_round_do_not_grow_the_plan():
    """Попытки исчерпаны — задача завершается, план не растёт задним числом."""
    state = task_state.start_validation(_executing(sections=3))
    for number in range(config.MAX_VALIDATION_ATTEMPTS):
        state = task_state.validation_verdict(
            state,
            [task_state.TaskIssue(item=1, text="мало")],
            (f"Дописать правила {number}",),
        )

    assert state.tasks[0].status is TaskStatus.DONE
    assert "Дописать правила 0" in state.tasks[0].plan  # пришла с прошлого круга
    assert f"Дописать правила {config.MAX_VALIDATION_ATTEMPTS - 1}" not in state.tasks[0].plan


# --- результат задачи: файл в каталоге tasks/ ---


def test_result_file_name_is_a_number_and_a_latin_slug():
    state = _planned()

    assert task_state.result_file_name(state.tasks[0], 2) == "2-razrabotat-igru-pro-ulitok.md"


def test_result_markdown_carries_the_goal_the_sections_and_the_issues():
    state = _done(task_state.add_task(_state(), "Вторая"))
    task = state.tasks[0]

    text = task_state.result_markdown(task)

    assert text.startswith("# Разработать игру про улиток")
    assert "_Задача решена: разделов — 3" in text
    assert "## 1. Тема и жанр" in text
    assert "Раздел 3" in text


def test_result_markdown_lists_unresolved_issues():
    state = task_state.validation_verdict(
        task_state.start_validation(_executing(sections=3)),
        [task_state.TaskIssue(item=2, text="ход игрока описан слабо")],
    )

    text = task_state.result_markdown(state.tasks[0])

    assert "_Задача завершена с замечаниями" in text
    assert "## Замечания проверки" in text
    assert "- ход игрока описан слабо" in text


def test_write_result_puts_the_file_into_the_directory(tmp_path):
    state = _done()
    directory = tmp_path / "tasks"

    path = task_state.write_result(directory, 1, state.tasks[0])

    assert path == directory / "1-razrabotat-igru-pro-ulitok.md"
    assert "## 1. Тема и жанр" in path.read_text(encoding="utf-8")


def test_write_result_error_does_not_break_the_run(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("не каталог", encoding="utf-8")

    assert task_state.write_result(blocked / "tasks", 1, _done().tasks[0]) is None
