"""Конвейер задачи: этапы, запросы, разбор ответов и отказы — на фальшивом запросе к модели."""

import json
from types import SimpleNamespace

from core import config, task_pipeline, task_state
from core.api_client import APIError, AnswerMeta
from core.task_pipeline import (
    PHASE_EXECUTE,
    PHASE_PLAN,
    PHASE_VALIDATE,
    TaskPipeline,
    parse_plan_response,
    parse_validation_response,
)
from core.task_state import Stage, TaskStatus, TaskStore

PLAN_JSON = '{"items": ["Тема и жанр", "Ход игрока", "Подсчёт очков"]}'
SECOND_PLAN_JSON = '{"items": ["Правила", "Компоненты", "Плейтест"]}'
OK_JSON = '{"ok": true, "issues": []}'


def _meta(content: str, finish_reason: str = "") -> AnswerMeta:
    return AnswerMeta(
        content=content,
        model="deepseek-v4-flash",
        elapsed_seconds=1.0,
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        cost_usd=0.001,
        finish_reason=finish_reason or None,
    )


class FakeAsk:
    """Фальшивый запрос: отдаёт заранее заготовленные ответы и записывает вызовы."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages, max_words, phase):
        self.calls.append(SimpleNamespace(messages=messages, max_words=max_words, phase=phase))
        if not self.responses:
            raise AssertionError(f"лишний запрос конвейера: {phase}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def phases(self):
        return [call.phase for call in self.calls]


def _pipeline(tmp_path, responses, goals=("Разработать игру про улиток",)):
    store = TaskStore(tmp_path / "task.json")
    state = store.state
    for goal in goals:
        state = task_state.add_task(state, goal)
    store.save(state)
    ask = FakeAsk(responses)
    return TaskPipeline(store, ask), ask, store


def _run_steps(pipeline, count: int):
    reports = []
    for _ in range(count):
        report = pipeline.step()
        if report is None:
            break
        reports.append(report)
    return reports


def _run_until_finished(pipeline, limit: int = 30):
    """Крутит конвейер до итога задачи: число шагов зависит от попыток проверки."""
    for _ in range(limit):
        report = pipeline.step()
        if report is None or report.finished_task:
            return report
    raise AssertionError("конвейер не дошёл до итога")


# --- планирование ---


def test_planning_step_asks_for_a_plan_and_waits_for_edits(tmp_path):
    pipeline, ask, store = _pipeline(tmp_path, [_meta(PLAN_JSON)])

    report = pipeline.step()

    assert ask.phases == [PHASE_PLAN]
    assert ask.calls[0].max_words == config.TASK_PLAN_MAX_WORDS
    assert report.needs_edits is True
    assert report.plan == ("Тема и жанр", "Ход игрока", "Подсчёт очков")
    assert report.stage is Stage.PLANNING
    assert pipeline.state.tasks[0].plan_round == 1
    assert pipeline.state.tasks[0].status is TaskStatus.RUNNING
    assert TaskStore(tmp_path / "task.json").state.tasks[0].plan == report.plan


def test_plan_request_carries_the_goal_and_the_item_range(tmp_path):
    pipeline, ask, _ = _pipeline(tmp_path, [_meta(PLAN_JSON)])

    pipeline.step()

    user_message = ask.calls[0].messages[-1]["content"]
    assert "улиток" in user_message
    if config.MIN_PLAN_ITEMS == config.MAX_PLAN_ITEMS:
        assert f"ровно {config.MIN_PLAN_ITEMS}" in user_message
    else:
        assert f"от {config.MIN_PLAN_ITEMS} до {config.MAX_PLAN_ITEMS}" in user_message
    assert f"до {config.TASK_PLAN_ITEM_MAX_CHARS} символов" in user_message


def test_edits_rebuild_the_plan_with_the_user_text(tmp_path):
    pipeline, ask, _ = _pipeline(tmp_path, [_meta(PLAN_JSON), _meta(SECOND_PLAN_JSON)])
    pipeline.step()

    pipeline.answer_edits("добавь пункт про подсчёт очков")
    report = pipeline.step()

    assert ask.phases == [PHASE_PLAN, PHASE_PLAN]
    request = ask.calls[1].messages[-1]["content"]
    assert "добавь пункт про подсчёт очков" in request
    assert "Тема и жанр" in request
    assert report.plan == ("Правила", "Компоненты", "Плейтест")
    assert pipeline.state.tasks[0].plan_round == 2
    assert report.notice == "правки приняты — план строится заново (круг 2)"


def test_empty_edits_move_the_task_to_execution(tmp_path):
    pipeline, ask, _ = _pipeline(tmp_path, [_meta(PLAN_JSON), _meta("Раздел")])
    pipeline.step()

    pipeline.answer_edits("   ")
    report = pipeline.step()

    assert ask.phases == [PHASE_PLAN, PHASE_EXECUTE]
    assert report.stage is Stage.EXECUTION


def test_plan_rounds_are_bounded_and_the_plan_is_taken_as_is(tmp_path):
    responses = [_meta(PLAN_JSON)]
    for round_number in range(2, config.MAX_PLAN_ROUNDS + 1):
        responses.append(_meta(SECOND_PLAN_JSON))
    responses.append(_meta("Раздел"))
    pipeline, ask, _ = _pipeline(tmp_path, responses)
    pipeline.step()
    for _ in range(config.MAX_PLAN_ROUNDS - 1):
        pipeline.answer_edits("ещё правка")
        pipeline.step()

    pipeline.answer_edits("правка сверх лимита")
    pipeline.step()

    assert ask.phases[-1] == PHASE_EXECUTE


def test_unparsable_plan_is_retried_and_then_fails_the_task(tmp_path):
    pipeline, ask, _ = _pipeline(
        tmp_path, [_meta("не JSON вовсе"), _meta("и это тоже"), _meta(PLAN_JSON)]
    )

    report = pipeline.step()

    assert ask.phases == [PHASE_PLAN, PHASE_PLAN]
    assert report.finished_task is True
    assert report.failure
    assert pipeline.state.tasks[0].status is TaskStatus.FAILED


def test_api_error_during_planning_fails_the_task_and_keeps_the_queue(tmp_path):
    pipeline, ask, _ = _pipeline(
        tmp_path,
        [APIError("нет сети"), _meta(SECOND_PLAN_JSON)],
        goals=("Первая", "Вторая"),
    )

    first = pipeline.step()
    second = pipeline.step()

    assert first.finished_task is True
    assert first.failure == "нет сети"
    assert pipeline.state.tasks[0].status is TaskStatus.FAILED
    assert pipeline.state.tasks[1].status is TaskStatus.RUNNING
    assert ask.phases == [PHASE_PLAN, PHASE_PLAN]
    assert "Вторая" in ask.calls[1].messages[-1]["content"]


# --- выполнение ---


def test_execution_writes_a_section_per_item_and_then_goes_to_validation(tmp_path):
    pipeline, ask, _ = _pipeline(
        tmp_path,
        [
            _meta(PLAN_JSON),
            _meta("Тема: гонки улиток."),
            _meta("Ход: одна улитка."),
            _meta("Очки: по одному."),
            _meta(OK_JSON),
        ],
    )
    pipeline.step()
    pipeline.answer_edits("")

    reports = _run_steps(pipeline, 5)

    assert ask.phases == [PHASE_PLAN, PHASE_EXECUTE, PHASE_EXECUTE, PHASE_EXECUTE, PHASE_VALIDATE]
    assert [section.text for section in pipeline.state.tasks[0].sections] == [
        "Тема: гонки улиток.",
        "Ход: одна улитка.",
        "Очки: по одному.",
    ]
    # Отчёт шага описывает точку, в которой задача оказалась после него.
    assert [report.step for report in reports] == [
        "2/3: «Ход игрока»",
        "3/3: «Подсчёт очков»",
        "все подзадачи выполнены — иду в Validation",
        f"попытка 1/{config.MAX_VALIDATION_ATTEMPTS}",
        "итог отправлен",
    ]
    assert reports[4].finished_task is True
    assert reports[4].sections == 3
    assert reports[4].issues == ()
    assert reports[4].expected_action == "очередь пуста — прогон остановлен"
    assert pipeline.state.finished is True


def test_execution_request_carries_the_item_and_the_written_sections(tmp_path):
    pipeline, ask, _ = _pipeline(
        tmp_path, [_meta(PLAN_JSON), _meta("Тема: гонки улиток."), _meta("Ход: одна улитка.")]
    )
    pipeline.step()
    pipeline.answer_edits("")
    pipeline.step()
    pipeline.step()

    request = ask.calls[2].messages[-1]["content"]
    assert "2. Ход игрока" in request
    assert f"не больше {config.TASK_SECTION_MAX_WORDS} слов" in request
    assert "Тема: гонки улиток." in request
    assert "не повторяй" in request.lower()


def test_failed_subtask_is_marked_and_the_rest_continues(tmp_path):
    pipeline, ask, _ = _pipeline(
        tmp_path,
        [_meta(PLAN_JSON), _meta("Раздел 1"), APIError("таймаут"), _meta("Раздел 3")],
    )
    pipeline.step()
    pipeline.answer_edits("")

    first = pipeline.step()
    second = pipeline.step()
    third = pipeline.step()

    assert first.step == "2/3: «Ход игрока»"
    assert "не выполнена" in second.notice
    assert pipeline.state.tasks[0].sections[1].failed == "таймаут"
    assert pipeline.state.tasks[0].sections[2].text == "Раздел 3"
    assert third.step == "все подзадачи выполнены — иду в Validation"


def test_truncated_answer_is_marked_and_reported_not_redone(tmp_path):
    """Обрезанный раздел не переделывают: работа сделана, а замечание о ней видно в итоге."""
    pipeline, ask, _ = _pipeline(
        tmp_path,
        [
            _meta(PLAN_JSON),
            _meta("Тема: гонки", finish_reason="length"),
            _meta("Ход: улитка."),
            _meta("Очки: по одному."),
            _meta(OK_JSON),
        ],
    )
    pipeline.step()
    pipeline.answer_edits("")

    truncated = pipeline.step()
    _run_steps(pipeline, 3)
    done = _run_steps(pipeline, 1)[0]

    assert "обрезан" in truncated.notice
    assert pipeline.state.tasks[0].sections[0].truncated is True
    assert done.finished_task is True
    assert any("обрезан" in issue for issue in done.issues)
    assert ask.phases == [PHASE_PLAN, PHASE_EXECUTE, PHASE_EXECUTE, PHASE_EXECUTE, PHASE_VALIDATE]


def test_validation_request_carries_the_artifact(tmp_path):
    pipeline, ask, _ = _pipeline(
        tmp_path,
        [_meta(PLAN_JSON), _meta("Раздел 1"), _meta("Раздел 2"), _meta("Раздел 3"), _meta(OK_JSON)],
    )
    pipeline.step()
    pipeline.answer_edits("")
    _run_steps(pipeline, 5)

    request = ask.calls[4].messages[-1]["content"]
    assert "## 1. Тема и жанр" in request
    assert "Раздел 3" in request
    asset = ask.calls[4].messages[0]["content"]
    assert "нельзя пользоваться" in asset  # проверка снисходительна к пригодному результату


def test_review_issues_send_the_task_back_to_execution(tmp_path):
    """Проверка назвала новую подзадачу: круг исправления выполняет только её."""
    issues_json = (
        '{"ok": false, "issues": [{"item": 0, "issue": "в артефакте нет подсчёта очков"}],'
        ' "items": ["Описать подсчёт очков"]}'
    )
    pipeline, ask, _ = _pipeline(
        tmp_path,
        [
            _meta(PLAN_JSON),
            _meta("Раздел 1"),
            _meta("Раздел 2"),
            _meta("Раздел 3"),
            _meta(issues_json),
            _meta("Очки: по одному за улитку."),
            _meta(OK_JSON),
        ],
    )
    pipeline.step()
    pipeline.answer_edits("")
    _run_steps(pipeline, 4)

    verdict = _run_steps(pipeline, 1)[0]

    assert verdict.stage is Stage.EXECUTION
    assert pipeline.state.tasks[0].fixing == (3,)
    assert verdict.step == "4/4: «Описать подсчёт очков»"
    assert verdict.expected_action == "исправить по замечаниям: «Описать подсчёт очков»"

    fix = _run_steps(pipeline, 1)[0]
    request = ask.calls[-1].messages[-1]["content"]
    assert "в артефакте нет подсчёта очков" in request
    assert "назвала проверка" in request
    assert fix.step == "все подзадачи выполнены — иду в Validation"

    done = _run_steps(pipeline, 2)[-1]

    assert done.finished_task is True
    assert pipeline.state.tasks[0].sections[3].text == "Очки: по одному за улитку."


def test_issues_about_ready_sections_finish_the_task_without_rework(tmp_path):
    """Замечание к готовому разделу не запускает повторного выполнения: задача завершается с ним."""
    issues_json = '{"ok": false, "issues": [{"item": 2, "issue": "ход игрока описан формально"}]}'
    pipeline, ask, _ = _pipeline(
        tmp_path,
        [
            _meta(PLAN_JSON),
            _meta("Раздел 1"),
            _meta("Раздел 2"),
            _meta("Раздел 3"),
            _meta(issues_json),
        ],
    )
    pipeline.step()
    pipeline.answer_edits("")
    _run_steps(pipeline, 4)

    verdict = _run_steps(pipeline, 1)[0]

    assert verdict.finished_task is True
    assert verdict.issues == ("ход игрока описан формально",)
    assert ask.phases == [PHASE_PLAN, PHASE_EXECUTE, PHASE_EXECUTE, PHASE_EXECUTE, PHASE_VALIDATE]
    assert pipeline.state.tasks[0].sections[1].text == "Раздел 2"  # раздел не переделывался


def test_unavailable_review_keeps_the_deterministic_verdict(tmp_path):
    pipeline, ask, _ = _pipeline(
        tmp_path,
        [_meta(PLAN_JSON), _meta("Раздел 1"), _meta("Раздел 2"), _meta("Раздел 3"), APIError("проверка недоступна"), APIError("проверка недоступна")],
    )
    pipeline.step()
    pipeline.answer_edits("")
    _run_steps(pipeline, 4)

    done = _run_steps(pipeline, 1)[0]

    assert done.finished_task is True
    assert done.sections == 3
    assert "модельная проверка недоступна" in done.notice


def test_unresolved_issues_finish_the_task_with_a_report(tmp_path):
    issues_json = (
        '{"ok": false, "issues": [{"item": 0, "issue": "ход игрока описан формально"}],'
        ' "items": ["Дописать ход игрока"]}'
    )
    replies = [_meta(PLAN_JSON), _meta("Раздел 1"), _meta("Раздел 2"), _meta("Раздел 3")]
    for _ in range(config.MAX_VALIDATION_ATTEMPTS):
        replies.append(_meta(issues_json))
        replies.append(_meta("Ход игрока: подробно."))
    pipeline, ask, _ = _pipeline(tmp_path, replies)
    pipeline.step()
    pipeline.answer_edits("")
    _run_steps(pipeline, 4)
    last = _run_until_finished(pipeline)

    assert last.finished_task is True
    assert last.issues == ("ход игрока описан формально",)
    assert "попытки проверки исчерпаны" in last.notice
    assert last.result_path
    assert pipeline.state.tasks[0].status is TaskStatus.DONE
    assert pipeline.state.tasks[0].attempt == config.MAX_VALIDATION_ATTEMPTS


# --- очередь и завершение ---


def test_second_task_is_planned_right_after_the_first(tmp_path):
    pipeline, ask, _ = _pipeline(
        tmp_path,
        [
            _meta(PLAN_JSON),
            _meta("Раздел 1"),
            _meta("Раздел 2"),
            _meta("Раздел 3"),
            _meta(OK_JSON),
            _meta(SECOND_PLAN_JSON),
        ],
        goals=("Разработать игру про улиток", "Собрать подборку на вечер"),
    )
    pipeline.step()
    pipeline.answer_edits("")
    _run_steps(pipeline, 5)

    assert pipeline.state.tasks[0].status is TaskStatus.DONE
    assert pipeline.state.tasks[1].status is TaskStatus.PENDING

    next_report = pipeline.step()

    assert next_report.goal == "Собрать подборку на вечер"
    assert next_report.stage is Stage.PLANNING
    assert pipeline.state.tasks[1].plan_round == 1
    assert ask.calls[-1].phase == PHASE_PLAN


def test_empty_queue_returns_nothing(tmp_path):
    pipeline, ask, _ = _pipeline(
        tmp_path, [_meta(PLAN_JSON), _meta("Раздел 1"), _meta("Раздел 2"), _meta("Раздел 3"), _meta(OK_JSON)]
    )
    pipeline.step()
    pipeline.answer_edits("")
    _run_steps(pipeline, 5)

    assert pipeline.step() is None
    assert ask.phases == [PHASE_PLAN, PHASE_EXECUTE, PHASE_EXECUTE, PHASE_EXECUTE, PHASE_VALIDATE]


def test_step_without_any_task_returns_nothing(tmp_path):
    pipeline, _, _ = _pipeline(tmp_path, [], goals=())

    assert pipeline.step() is None


# --- разбор ответов ---


def test_parse_plan_accepts_json_in_text_and_a_bare_list():
    assert parse_plan_response('Вот план:\n{"items": ["раз", "два"]}\nГотово.') == ("раз", "два")
    assert parse_plan_response('["раз", "два"]') == ("раз", "два")
    assert parse_plan_response('{"items": ["раз", "  ", 5, "два"]}') == ("раз", "два")
    assert parse_plan_response("никакого JSON") == ()
    assert parse_plan_response("") == ()


def test_parse_plan_clips_items_and_the_total(tmp_path):
    long_item = "ц" * (config.TASK_PLAN_ITEM_MAX_CHARS + 10)
    many = {"items": [f"пункт {n}" for n in range(config.MAX_PLAN_ITEMS + 5)]}

    assert len(parse_plan_response(json.dumps(many))) == config.MAX_PLAN_ITEMS
    assert len(parse_plan_response('{"items": ["' + long_item + '"]}')[0]) == config.TASK_PLAN_ITEM_MAX_CHARS


def test_parse_validation_reads_ok_issues_and_new_items():
    ok, issues, items = parse_validation_response('{"ok": true, "issues": []}')
    assert ok is True and issues == () and items == ()

    ok, issues, items = parse_validation_response(
        '{"ok": false, "issues": [{"item": 2, "issue": "мало"}, "общее замечание"],'
        ' "items": ["Описать подсчёт очков", "  "]}'
    )
    assert ok is False
    assert [(issue.item, issue.text) for issue in issues] == [(2, "мало"), (0, "общее замечание")]
    assert items == ("Описать подсчёт очков",)

    ok, issues, items = parse_validation_response('{"ok": false}')
    assert ok is False and len(issues) == 1 and items == ()

    assert parse_validation_response("не JSON") is None
    assert parse_validation_response('{"issues": []}') is None


def test_validation_can_ask_for_a_new_subtask_and_it_is_executed(tmp_path):
    """Новая подзадача от проверки встраивается в план и выполняется как остальные."""
    verdict_json = (
        '{"ok": false, "issues": [{"item": 0, "issue": "нет подсчёта очков"}],'
        ' "items": ["Описать подсчёт очков и конец партии"]}'
    )
    pipeline, ask, _ = _pipeline(
        tmp_path,
        [
            _meta(PLAN_JSON),
            _meta("Раздел 1"),
            _meta("Раздел 2"),
            _meta("Раздел 3"),
            _meta(verdict_json),
            _meta("Очки: по одному за улитку."),
            _meta(OK_JSON),
        ],
    )
    pipeline.step()
    pipeline.answer_edits("")
    _run_steps(pipeline, 4)

    verdict = _run_steps(pipeline, 1)[0]

    assert verdict.stage is Stage.EXECUTION
    assert pipeline.state.tasks[0].plan[-1] == "Описать подсчёт очков и конец партии"
    assert verdict.step == "4/4: «Описать подсчёт очков и конец партии»"

    new_section = _run_steps(pipeline, 1)[0]
    request = ask.calls[-1].messages[-1]["content"]
    assert "Описать подсчёт очков и конец партии" in request
    assert "нет подсчёта очков" in request
    assert new_section.step == "все подзадачи выполнены — иду в Validation"

    done = _run_steps(pipeline, 2)[-1]
    assert done.finished_task is True
    assert len(pipeline.state.tasks[0].sections) == 4


# --- переходы конвейера через шлюз ---


def _accepted_path(task):
    return [(entry.source, entry.target) for entry in task.transitions if entry.accepted]


def test_full_run_walks_the_stages_in_order(tmp_path):
    """Прогон идёт по таблице переходов: пропустить этап конвейер не может (день 15)."""
    pipeline, _, _ = _pipeline(
        tmp_path,
        [_meta(PLAN_JSON), _meta("Раздел 1"), _meta("Раздел 2"), _meta("Раздел 3"), _meta(OK_JSON)],
    )
    pipeline.step()
    pipeline.answer_edits("")
    _run_until_finished(pipeline)

    task = pipeline.state.tasks[0]
    assert task.stage is Stage.DONE
    assert _accepted_path(task) == [
        (Stage.PLANNING, Stage.EXECUTION),
        (Stage.EXECUTION, Stage.VALIDATION),
        (Stage.VALIDATION, Stage.DONE),
    ]
    assert all(entry.reason for entry in task.transitions)


def test_fix_round_returns_through_the_gate(tmp_path):
    """Замечание проверки возвращает задачу в Execution — разрешённый таблицей переход."""
    pipeline, _, _ = _pipeline(
        tmp_path,
        [
            _meta(PLAN_JSON),
            _meta("Раздел 1"),
            _meta(""),
            _meta(""),
            _meta("Раздел 3"),
            _meta('{"ok": false, "issues": ["второй раздел пуст"]}'),
            _meta("Раздел 2"),
            _meta(OK_JSON),
        ],
    )
    pipeline.step()
    pipeline.answer_edits("")
    _run_until_finished(pipeline)

    task = pipeline.state.tasks[0]
    assert (Stage.VALIDATION, Stage.EXECUTION) in _accepted_path(task)
    assert _accepted_path(task)[-1] == (Stage.VALIDATION, Stage.DONE)


def test_replan_keeps_the_task_in_planning_without_a_transition(tmp_path):
    """Новый круг плана — не смена этапа: задача остаётся на Planning, журнал пуст."""
    pipeline, _, _ = _pipeline(tmp_path, [_meta(PLAN_JSON), _meta(SECOND_PLAN_JSON)])
    pipeline.step()
    pipeline.answer_edits("добавь подсчёт очков")
    pipeline.step()

    task = pipeline.state.tasks[0]
    assert task.stage is Stage.PLANNING
    assert task.transitions == ()


def test_transitions_are_saved_before_the_next_operation(tmp_path):
    pipeline, _, _ = _pipeline(tmp_path, [_meta(PLAN_JSON), _meta("Раздел 1")])
    pipeline.step()
    pipeline.answer_edits("")
    pipeline.step()

    stored = TaskStore(tmp_path / "task.json").state.tasks[0]
    assert _accepted_path(stored) == [(Stage.PLANNING, Stage.EXECUTION)]
