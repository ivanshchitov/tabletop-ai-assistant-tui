"""Агент Tabletop AI Assistant: память диалога, стратегии контекста и пересылка в LLM.

Агент — единственное место, где решается, что и как отправляется модели. Он владеет всей
памятью диалога: логом ходов сессии (он только растёт — ходы из него не удаляются), сжатым
резюме, блоком фактов, ветками и файлом истории, — и собирает запрос по активной стратегии
управления контекстом. Стратегия — это вид на лог, а не его порча: переключение стратегии
ничего не теряет. Терминальный интерфейс только показывает результат и получает от агента
снимок состояния контекста: агент ничего не печатает, ошибки API отдаёт исключением.
"""

from dataclasses import dataclass, field
from pathlib import Path
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import (
    config,
    context_compressor,
    context_strategies,
    invariants,
    memory_layers,
    prompts,
    task_pipeline,
    task_state,
)
from .usage import SessionLedger, SessionUsage, estimate_tokens
from .answer_settings import AnswerFormat, AnswerSettings, ContextStrategy
from .api_client import APIClient, AnswerMeta, APIError
from .history_manager import HistoryManager
from .long_term_memory import LongTermMemory
from .task_pipeline import TaskStepReport
from .task_state import Stage, TaskItem, TaskState, TaskStore
from .user_profile import InterviewState, ProfileStore, UserProfile, clip_value, profile_message


class RequestPhase(Enum):
    """Фаза запроса агента: вспомогательный запрос стратегии или сам вопрос.

    Агент не знает ни rich, ни слов отображения: слушатель фаз получает перечисление,
    терминальный слой маппит его на подписи индикатора.
    """

    COMPRESSION = "compression"
    FACTS_UPDATE = "facts_update"
    REQUEST = "request"
    # Фазы конвейера задачи: значения совпадают с именами фаз конвейера, поэтому переход
    # «фаза конвейера → уведомление интерфейса» — одно преобразование, без таблицы.
    TASK_PLAN = task_pipeline.PHASE_PLAN
    TASK_EXECUTE = task_pipeline.PHASE_EXECUTE
    TASK_VALIDATE = task_pipeline.PHASE_VALIDATE


@dataclass
class CompressionReport:
    """Что свернул последний суммаризатор: размер пакета и метрики его запроса."""

    messages: int
    exchanges: int
    meta: AnswerMeta


@dataclass
class FactsReport:
    """Итог последнего обновления блока фактов: удалось ли и сколько ключей в блоке."""

    updated: bool
    keys: int
    error: Optional[str] = None


@dataclass
class ContextReport:
    """Снимок состояния контекста для отчётов интерфейса (без обращения к модели)."""

    strategy: ContextStrategy
    window: int
    max_session_tokens: int
    log_exchanges: int
    request_turns: int
    summary_covers: int
    has_summary: bool
    facts: Dict[str, str]
    branch: str
    branches: Tuple[Tuple[str, int], ...]
    tokens_estimate: int


@dataclass
class MemoryReport:
    """Снимок слоёв памяти для отчётов интерфейса (без обращения к модели).

    Хранилища отдаются путями — интерфейс показывает, какой файл держит слой, и не знает,
    как слой устроен внутри. Правила маршрутизации идут текстом: их печатает отчёт, а не
    повторяет у себя таблицу правил.
    """

    short_term_exchanges: int
    working: Tuple[memory_layers.MemoryRecord, ...]
    long_term: Tuple[memory_layers.MemoryRecord, ...]
    history_store: str
    long_term_store: str
    rules: Tuple[str, ...]


@dataclass
class ProfileReport:
    """Снимок персонализации для отчётов интерфейса (без обращения к модели).

    Разделы идут парами «подпись — содержимое», потому что профиль в запрос уходит подписями:
    отчёт показывает ровно то, что видит модель, а не машинные имена раздела.
    """

    active_name: str
    sections: Tuple[Tuple[str, str], ...]
    names: Tuple[str, ...]
    profile_store: str


@dataclass(frozen=True)
class InvariantsCheck:
    """Результат проверки последнего ответа на инварианты: что нарушил первый ответ, был ли
    повторный запрос, отклонён ли итог (и что нарушил повторный ответ)."""

    violations: Tuple[invariants.Violation, ...]
    retried: bool
    rejected: bool
    final_violations: Tuple[invariants.Violation, ...] = ()


@dataclass
class InvariantsReport:
    """Снимок таблицы инвариантов для отчёта интерфейса (без обращения к модели)."""

    invariants: Tuple[invariants.Invariant, ...]


@dataclass
class TaskQueueEntry:
    """Строка очереди задач для отчёта: цель, статус, место в работе и объём артефакта."""

    goal: str
    status: str
    stage: str
    step: str
    artifact_chars: int


@dataclass
class TransitionEntry:
    """Запись журнала переходов для отчёта: этапы ярлыками, исход и причина."""

    source: str
    target: str
    accepted: bool
    reason: str


@dataclass
class StageRequestResult:
    """Итог запроса перехода пользователем: что просили, что вышло и что вообще разрешено."""

    accepted: bool
    source: str
    target: str
    reason: str
    allowed: Tuple[str, ...]
    known: bool = True


@dataclass
class TaskReport:
    """Снимок состояния задачи для интерфейса (без обращения к модели).

    Терминальный слой рендерит снимок и не знает ни правил автомата, ни файла состояния: этап,
    текущий шаг и ожидаемое действие уже посчитаны, план приходит с отметками выполнения.
    """

    queue: Tuple[TaskQueueEntry, ...]
    active_number: int
    stages: Tuple[str, ...]
    active_stage: str
    current_step: str
    expected_action: str
    plan: Tuple[Tuple[str, str], ...]
    artifact_sections: Tuple[str, ...]
    artifact_chars: int
    issues: Tuple[str, ...]
    paused: bool
    task_store: str
    allowed_transitions: Tuple[str, ...] = ()
    transitions: Tuple[TransitionEntry, ...] = ()


@dataclass
class AgentConfig:
    """Единый конфиг агента: настройки ответа сессии и модель."""

    settings: AnswerSettings = field(default_factory=AnswerSettings)
    model: str = config.DEFAULT_MODEL

    # Плоский доступ на чтение к параметрам настроек ответа.
    @property
    def format(self) -> AnswerFormat:
        return self.settings.format

    @property
    def strategy(self) -> ContextStrategy:
        return self.settings.context_strategy

    @property
    def max_words(self) -> int:
        return self.settings.max_words

    @property
    def list_limit(self) -> int:
        return self.settings.list_limit

    @property
    def temperature(self) -> float:
        return self.settings.temperature


class TabletopAgent:
    """Вопрос -> контекст по активной стратегии -> сборка сообщений -> LLM -> ответ с метриками."""

    def __init__(
        self,
        client: Optional[APIClient],
        settings: Optional[AnswerSettings] = None,
        model: Optional[str] = None,
        history: Optional[HistoryManager] = None,
        long_term: Optional[LongTermMemory] = None,
        profile: Optional[ProfileStore] = None,
        task: Optional[TaskStore] = None,
        task_results_dir: Optional[Path] = None,
    ) -> None:
        self.client = client
        self.config = AgentConfig(
            settings=settings if settings is not None else AnswerSettings(),
            model=model if model is not None else config.DEFAULT_MODEL,
        )
        self.history = history if history is not None else HistoryManager()
        # Долговременный слой памяти — свой файл: сведения о пользователе переживают и /clear,
        # и перезапуск, поэтому конверт истории его не держит.
        self.long_term = long_term if long_term is not None else LongTermMemory()
        # Персонализация — тоже свой файл: профиль настраивает сам пользователь, он не выводится
        # правилами из реплик, и его не касается ни /clear, ни очистка слоёв памяти.
        self.profile = profile if profile is not None else ProfileStore()
        # Очередь задач — снова свой файл: задача переживает и `/clear`, и перезапуск, а
        # конвейер её ведения живёт здесь же — решение о том, что уходит модели, принимает
        # агент, а терминальный слой только показывает снимок и рисует панель.
        self.task = task if task is not None else TaskStore()
        self._pipeline = task_pipeline.TaskPipeline(
            self.task,
            self._ask_task,
            task_results_dir if task_results_dir is not None else config.TASK_RESULTS_DIR,
        )
        # Слушатель фаз на время шага конвейера: запросы конвейера сообщают те же фазы,
        # что и вспомогательные запросы стратегий.
        self._phase_listener: Optional[Callable[[RequestPhase], None]] = None
        # Рабочий слой — данные текущей задачи (цель и ограничения): его место в конверте
        # истории, потому что жизнь слоя равна жизни диалога.
        self._working: Dict[str, str] = {}
        # Решение маршрута последней реплики — для журнальной строки интерфейса.
        self._last_routing: Tuple[memory_layers.MemoryRecord, ...] = ()
        # Лог ходов сессии: пары user/assistant успешных обменов, append-only. system в логе
        # не хранится, ходы не удаляются — стратегия лишь выбирает, что из лога отправить.
        self._turns: List[Dict[str, str]] = []
        # Память стратегии сжатого резюме: дайджест, число покрытых им обменов файла и
        # число обменов лога, которые резюме накрывает сейчас (после рестарта лог содержит
        # только несвёрнутый хвост файла, поэтому эти счётчики расходятся).
        self._summary: Optional[str] = None
        self._summary_covers: int = 0
        self._log_covered: int = 0
        # Память стратегии фактов: блок «ключ — значение», очередь ещё не переработанных
        # извлекателем сообщений пользователя (сбой не должен терять их) и число обменов лога,
        # уже отданных извлекателю — по нему очередь пополняется из лога, когда стратегия
        # включается посреди диалога.
        self._facts: Dict[str, str] = {}
        self._facts_pending: List[str] = []
        self._facts_covered: int = 0
        # Память стратегии веток: ветки как индексы в общем логе.
        self._branches = context_strategies.BranchTree(self._turns)
        self._last_compression: Optional[CompressionReport] = None
        self._last_facts: Optional[FactsReport] = None
        self._last_result: Optional[AnswerMeta] = None
        self._last_invariants: Optional[InvariantsCheck] = None
        # Учёт расхода сессии: каждый успешный запрос к API (вопрос и вспомогательные).
        self._ledger = SessionLedger()
        # Память агента — своя: контекст восстанавливается из файла истории сразу при
        # создании, «забыть вызвать» восстановление невозможно.
        self.restore_context()

    @property
    def settings(self) -> AnswerSettings:
        """Настройки ответа и контекста внутри конфига агента."""
        return self.config.settings

    @settings.setter
    def settings(self, value: AnswerSettings) -> None:
        self.config.settings = value

    @property
    def model(self) -> str:
        return self.config.model

    @model.setter
    def model(self, value: str) -> None:
        self.config.model = value

    @property
    def last_result(self) -> Optional[AnswerMeta]:
        """Метрики последнего запроса к API: время, токены, стоимость (только чтение)."""
        return self._last_result

    @property
    def last_compression(self) -> Optional[CompressionReport]:
        """Отчёт последнего сжатия: сколько сообщений/обменов свёрнуто в резюме."""
        return self._last_compression

    @property
    def last_facts(self) -> Optional[FactsReport]:
        """Отчёт последнего обновления фактов: удалось ли и сколько ключей в блоке."""
        return self._last_facts

    @property
    def last_invariants(self) -> Optional[InvariantsCheck]:
        """Результат проверки последнего ответа на инварианты — для строк журнала интерфейса."""
        return self._last_invariants

    @property
    def last_routing(self) -> Tuple[memory_layers.MemoryRecord, ...]:
        """Записи, сделанные в слои памяти последней репликой пользователя (только чтение)."""
        return self._last_routing

    @property
    def working_memory(self) -> Dict[str, str]:
        """Рабочая память задачи: цель и ограничения текущего диалога (только чтение)."""
        return dict(self._working)

    @property
    def long_term_memory(self) -> LongTermMemory:
        """Хранилище долговременной памяти — для снимка и операций интерфейса."""
        return self.long_term

    @property
    def session_usage(self) -> SessionUsage:
        """Итоги сессии: накопленные токены и стоимость всех успешных запросов."""
        return self._ledger.usage

    @property
    def branches(self) -> Tuple[Tuple[str, int], ...]:
        """Ветки диалога: имя и число обменов в каждой (для панели /branches)."""
        return self._branches.branches()

    @property
    def active_branch(self) -> str:
        """Имя активной ветки диалога."""
        return self._branches.active_name

    def ask(
        self,
        question: str,
        on_phase: Optional[Callable[[RequestPhase], None]] = None,
    ) -> AnswerMeta:
        """Задаёт вопрос с контекстом сессии; при ошибке API поднимает APIError.

        Перед вопросом собирается контекст активной стратегии: суммаризатор сворачивает
        старейшие ходы в резюме, извлекатель обновляет блок фактов. Сбой вспомогательного
        запроса стратегии фактов вопрос не отменяет — он уходит с прежним блоком.
        """
        user_prompt = prompts.build_user_prompt(question, self.config.settings)
        # Маршрут слоёв считается до сборки запроса: запись, сделанная текущей репликой, должна
        # быть видна модели уже в этом запросе. Запросов к модели маршрут не делает.
        self._route_memory(question)
        skip = self._prepare_context(question, user_prompt, on_phase)
        self._signal(on_phase, RequestPhase.REQUEST)
        messages = self._build_messages(user_prompt, skip)
        meta = self._ask_question(messages)
        meta = self._enforce_invariants(messages, meta, on_phase)
        self._remember(user_prompt, meta.content)
        # Долговременная память: пара «вопрос–ответ» с метриками — на диск сразу после ответа.
        self.history.add(question, meta.content, usage=self._usage_block(meta))
        return meta

    def _ask_question(self, messages: List[Dict[str, str]]) -> AnswerMeta:
        """Запрос вопроса с настройками сессии; расход учтён, метрики — в `last_result`."""
        meta = self.client.ask_with_usage_messages(
            messages,
            max_tokens=config.max_tokens_for_words(self.config.max_words),
            temperature=self.config.temperature,
            model=self.config.model,
        )
        self._last_result = meta
        self._ledger.record(meta)
        return meta

    def _enforce_invariants(
        self,
        messages: List[Dict[str, str]],
        meta: AnswerMeta,
        on_phase: Optional[Callable[[RequestPhase], None]],
    ) -> AnswerMeta:
        """Проверка ответа на инварианты кодом: повтор с перечнем нарушений, затем отклонение.

        Повторный запрос — продолжение того же диалога: ответ модели ходом assistant и перечень
        нарушений ходом user, чтобы модель видела, что именно не так. Нарушивший и повторный ответ
        отклоняется: вместо него — отказ приложения, который называет инвариант и найденное слово;
        в стек и в историю ложится только то, что увидел пользователь. Ошибка повторного запроса
        поднимается наверх, как ошибка любого запроса: ничего не записано.
        """
        violations = invariants.check_answer(meta.content)
        if not violations:
            self._last_invariants = InvariantsCheck(violations=(), retried=False, rejected=False)
            return meta
        final = violations
        for _ in range(config.INVARIANT_RETRIES):
            self._signal(on_phase, RequestPhase.REQUEST)
            retry = messages + [
                {"role": "assistant", "content": meta.content},
                {"role": "user", "content": invariants.retry_prompt(final)},
            ]
            meta = self._ask_question(retry)
            final = invariants.check_answer(meta.content)
            if not final:
                self._last_invariants = InvariantsCheck(violations, retried=True, rejected=False)
                return meta
        self._last_invariants = InvariantsCheck(violations, retried=True, rejected=True, final_violations=final)
        return AnswerMeta(
            content=invariants.refusal_text(final),
            model=meta.model,
            elapsed_seconds=meta.elapsed_seconds,
            prompt_tokens=meta.prompt_tokens,
            completion_tokens=meta.completion_tokens,
            total_tokens=meta.total_tokens,
            cost_usd=meta.cost_usd,
            finish_reason=meta.finish_reason,
        )

    def memory_report(self) -> MemoryReport:
        """Снимок слоёв памяти для отчётов интерфейса.

        Отдаёт краткосрочный слой (число обменов диалога), рабочую и долговременную память с
        категориями записей, хранилища обоих слоёв и перечень правил маршрутизации. Ни одного
        обращения к модели не делает.
        """
        return MemoryReport(
            short_term_exchanges=len(self._turns) // 2,
            working=memory_layers.records_from_block(memory_layers.WORKING, self._working),
            long_term=self.long_term.records(),
            history_store=str(self.history.path),
            long_term_store=str(self.long_term.path),
            rules=memory_layers.describe_rules(),
        )

    def profile_report(self) -> ProfileReport:
        """Снимок персонализации для отчётов интерфейса.

        Отдаёт имя активного профиля, его заполненные разделы, имена всех профилей файла и путь
        файла. Ни одного обращения к модели не делает, файл профилей читает сам и наружу его
        структуру не выпускает — интерфейс рендерит снимок.
        """
        active = self.profile.active()
        return ProfileReport(
            active_name=active.name,
            sections=active.entries,
            names=self.profile.names(),
            profile_store=str(self.profile.path),
        )

    def invariants_report(self) -> InvariantsReport:
        """Снимок таблицы инвариантов для отчёта интерфейса; запросов к модели не делает."""
        return InvariantsReport(invariants=invariants.INVARIANTS)

    def add_task(self, goal: str) -> bool:
        """Ставит задачу в очередь; False — цель пуста, задача не заведена."""
        state = task_state.add_task(self.task.state, goal)
        if len(state.tasks) == len(self.task.state.tasks):
            return False
        self.task.save(state)
        return True

    def drop_tasks(self) -> None:
        """Снимает очередь задач: только это её и очищает — `/clear` задачу не трогает."""
        self.task.save(task_state.drop_all(self.task.state))

    def pause_tasks(self) -> None:
        """Ставит прогон на паузу: срабатывает на границе операции, состояние уже на диске."""
        self.task.save(task_state.set_paused(self.task.state, True))

    def resume_tasks(self) -> None:
        """Снимает паузу: прогон продолжается с сохранённого этапа и шага."""
        self.task.save(task_state.set_paused(self.task.state, False))

    @property
    def tasks_paused(self) -> bool:
        """Стоит ли прогон на паузе (только чтение)."""
        return self.task.state.paused

    def request_stage(self, name: str) -> StageRequestResult:
        """Запрос перехода активной задачи в названный этап — через тот же шлюз, что и у конвейера.

        Терминальный слой печатает результат и не знает ни таблицы переходов, ни предусловий: та же
        граница изоляции, что у `context_report()` и `memory_report()`. Обращений к модели нет.
        """
        state = self.task.state
        task = state.active
        source = task_state.STAGE_LABELS[task.stage] if task is not None else ""
        allowed = self._allowed_transitions(state)
        target = task_state.parse_stage(name)
        if target is None:
            return StageRequestResult(
                accepted=False,
                source=source,
                target=str(name).strip(),
                reason=f"этапа «{str(name).strip()}» в автомате нет",
                allowed=allowed,
                known=False,
            )
        result = task_state.transition(state, target, "переход по просьбе пользователя")
        self.task.save(result.state)
        return StageRequestResult(
            accepted=result.accepted,
            source=source,
            target=task_state.STAGE_LABELS[target],
            reason=result.reason,
            allowed=self._allowed_transitions(result.state) if result.accepted else allowed,
        )

    @staticmethod
    def _allowed_transitions(state: TaskState) -> Tuple[str, ...]:
        task = state.active
        if task is None:
            return ()
        return tuple(
            task_state.STAGE_LABELS[stage] for stage in task_state.allowed_transitions(task.stage)
        )

    def task_step(self, on_phase: Optional[Callable[[RequestPhase], None]] = None) -> Optional[TaskStepReport]:
        """Одна операция конвейера задачи; None — незавершённых задач нет.

        Уведомления о фазах запросов конвейера уходят тому же слушателю, что и фазы вопроса:
        интерфейс показывает по ним подписи индикатора. Расход каждого запроса конвейера
        записывается в общий накопитель сессии.
        """
        self._phase_listener = on_phase
        try:
            return self._pipeline.step()
        finally:
            self._phase_listener = None

    def task_answer_edits(self, text: str) -> None:
        """Принимает ответ пользователя о правках к плану задачи."""
        self._pipeline.answer_edits(text)

    def task_report(self) -> TaskReport:
        """Снимок состояния задачи для интерфейса.

        Отдаёт очередь со статусами, этап, текущий шаг, ожидаемое действие, план с отметками
        выполнения, разделы и объём артефакта, замечания проверки, признак паузы и путь файла
        состояния. Ни одного обращения к модели не делает; интерфейс рендерит снимок и не читает
        ни файл состояния, ни внутренние структуры агента.
        """
        state = self.task.state
        task = state.active
        queue = tuple(
            TaskQueueEntry(
                goal=item.goal,
                status=item.status.value,
                stage=task_state.STAGE_LABELS[item.stage],
                step=state.current_step if index == state.active_index else "—",
                artifact_chars=sum(
                    len(section.text) for section in item.sections if section.text.strip()
                ),
            )
            for index, item in enumerate(state.tasks)
        )
        if task is None:
            return TaskReport(
                queue=queue,
                active_number=0,
                stages=tuple(task_state.STAGE_LABELS[stage] for stage in task_state.STAGES),
                active_stage="",
                current_step=state.current_step,
                expected_action=state.expected_action,
                plan=(),
                artifact_sections=(),
                artifact_chars=0,
                issues=(),
                paused=state.paused,
                task_store=str(self.task.path),
                allowed_transitions=(),
                # Незавершённых задач нет: показываем путь последней — иначе жизненный цикл
                # задачи исчезал бы с экрана ровно тогда, когда он пройден целиком.
                transitions=_transition_entries(state.tasks[-1]) if state.tasks else (),
            )
        written = [section for section in task.sections if section.text.strip()]
        return TaskReport(
            queue=queue,
            active_number=state.active_index + 1,
            stages=tuple(task_state.STAGE_LABELS[stage] for stage in task_state.STAGES),
            active_stage=task_state.STAGE_LABELS[task.stage],
            current_step=state.current_step,
            expected_action=state.expected_action,
            plan=tuple(
                (item, _plan_mark(task, index)) for index, item in enumerate(task.plan)
            ),
            artifact_sections=tuple(section.item for section in written),
            artifact_chars=sum(len(section.text) for section in written),
            issues=tuple(
                f"{issue.item}. {issue.text}" if issue.item else issue.text
                for issue in task.issues
            ),
            paused=state.paused,
            task_store=str(self.task.path),
            allowed_transitions=self._allowed_transitions(state),
            transitions=_transition_entries(task),
        )

    def _ask_task(
        self, messages: List[Dict[str, str]], max_words: int, phase: str
    ) -> AnswerMeta:
        """Запрос конвейера задачи: модель сессии, без температуры сессии, прижатый потолок.

        Расход учитывается в общем накопителе, а ошибка API поднимается наверх — конвейер
        переводит её в состояние задачи, а не в исключение для интерфейса.
        """
        self._signal(self._phase_listener, RequestPhase(phase))
        # Инварианты — вторым сообщением, после system этапа: конвейер о них не знает, как не
        # знает о модели и потолке — что уходит модели, решает агент.
        messages = [messages[0], self._invariants_message(), *messages[1:]]
        meta = self.client.ask_with_usage_messages(
            messages,
            max_tokens=config.max_tokens_for_words(max_words),
            model=self.config.model,
        )
        self._last_result = meta
        self._ledger.record(meta)
        return meta

    def setup_profile(self, state: InterviewState) -> UserProfile:
        """Собирает профиль из ответов диалога настройки, сохраняет его и делает активным.

        Проходит диалог терминальный слой: он печатает вопросы и читает строки, а собирает профиль
        агент — поверх профиля с названным именем, если такой уже есть (пустой ответ оставляет
        раздел как был, поэтому повторная настройка — редактирование), и с именем по порядку, если
        вопрос об имени пропущен.
        """
        named = clip_value(state.answers[0]) if state.answers else ""
        base = self.profile.get(named) or UserProfile(name=named)
        profile = state.profile(base, self.profile.next_name())
        self.profile.save(profile)
        return profile

    def use_profile(self, name: str) -> Optional[UserProfile]:
        """Делает профиль активным; None — такого профиля нет (заводит профили только настройка)."""
        return self.profile.use(name)

    def forget_profile(self, name: str) -> bool:
        """Удаляет профиль по имени; False — такого профиля нет."""
        return self.profile.forget(name)

    def remember_goal(self, goal: str) -> None:
        """Записывает цель текущей задачи в рабочую память, заменяя прежнюю цель."""
        self._store_working(memory_layers.CATEGORY_GOAL, goal)

    def remember(self, text: str) -> memory_layers.MemoryRecord:
        """Явно записывает реплику в долговременную память и возвращает запись.

        Ключ и категорию даёт правило маршрутизации, если оборот распознан; иначе запись получает
        категорию заметки и собственный порядковый ключ — так заметки не вытесняют друг друга.
        """
        recognized = [
            record for record in memory_layers.route(text) if record.layer == memory_layers.LONG_TERM
        ]
        if not recognized:
            record = memory_layers.MemoryRecord(
                layer=memory_layers.LONG_TERM,
                category=memory_layers.CATEGORY_NOTE,
                key=self._next_note_key(),
                value=memory_layers.clip_value(text),
            )
            self.long_term.remember(record.key, record.value, record.category)
            return record
        for record in recognized:
            self.long_term.remember(record.key, record.value, record.category)
        return recognized[0]

    def forget(self, key: str) -> Optional[str]:
        """Удаляет запись по ключу из того слоя, где она лежит; None — такой записи нет."""
        if key in self._working:
            del self._working[key]
            self.history.set_working(self._working)
            return memory_layers.WORKING
        if self.long_term.forget(key):
            return memory_layers.LONG_TERM
        return None

    def forget_all(self) -> None:
        """Опустошает рабочую и долговременную память, не трогая ходы текущего диалога."""
        self._working = {}
        self.history.set_working(self._working)
        self.long_term.clear()

    def checkpoint(self) -> None:
        """Отмечает текущую позицию активной ветки — от неё создаётся следующая ветка."""
        self._branches.checkpoint()

    def new_branch(self) -> str:
        """Создаёт ветку от чекпоинта, делает её активной и возвращает имя."""
        return self._branches.new_branch()

    def switch_branch(self, name: str) -> bool:
        """Делает ветку активной; False — ветки с таким именем нет."""
        return self._branches.switch(name)

    def context_report(self) -> ContextReport:
        """Снимок состояния контекста для отчётов интерфейса.

        Отдаёт активную стратегию, границы окна и потолка, размер лога и ходов, попадающих в
        ближайший запрос, память стратегии (резюме, факты, ветки) и приближённую оценку
        токенов собираемого запроса. Ни одного обращения к модели не делает.
        """
        turns = self._view_turns()
        return ContextReport(
            strategy=self.config.strategy,
            window=self.config.settings.compress_after,
            max_session_tokens=self.config.settings.max_session_tokens,
            log_exchanges=len(self._turns) // 2,
            request_turns=len(turns),
            summary_covers=self._summary_covers,
            has_summary=bool(self._summary),
            facts=dict(self._facts),
            branch=self._branches.active_name,
            branches=self._branches.branches(),
            tokens_estimate=self._context_tokens(),
        )

    def reset(self) -> None:
        """Опустошает краткосрочную и рабочую память, но не долговременную (команда /clear).

        Краткосрочная память — лог ходов, рабочая — цель и ограничения задачи и память стратегии:
        всё это принадлежит текущему диалогу. Долговременная память — сведения о пользователе
        между сессиями — и профиль персонализации остаются и в памяти, и в своих файлах: профиль
        говорит, как отвечать этому человеку, а не что было в диалоге. Снять долговременную память
        можно только явно командой `/memory forget all`, профили — командой `/profile forget`.
        """
        self._turns.clear()
        self._summary = None
        self._summary_covers = 0
        self._log_covered = 0
        self._facts = {}
        self._facts_pending = []
        self._facts_covered = 0
        self._branches.reset()
        self._working = {}
        self._last_routing = ()
        self._last_compression = None
        self._last_facts = None
        self.history.clear()
        self._ledger.reset()

    def restore_context(self) -> None:
        """Засеивает контекст из собственной истории агента: резюме, факты и хвост обменов.

        Резюме и блок фактов занимают своё место в собираемом запросе; сообщения обменов, не
        покрытых резюме, становятся ходами лога (ход user собирается инструкциями активных
        настроек, ход assistant дословно). Непокрытые обмены догоняются суммаризатором при
        первом вопросе — тем же механизмом, что и живая сессия, без отдельного кода рестарта.
        """
        self._summary = self.history.summary
        self._summary_covers = self.history.summary_covers
        self._log_covered = 0
        self._facts = dict(self.history.facts)
        self._facts_pending = []
        # Рабочая память живёт в конверте истории: цель и ограничения задачи переживают
        # перезапуск, но не /clear. Долговременную память хранилище читает само.
        self._working = dict(self.history.working)
        dialogues = self.history.dialogues
        tail_records = max(0, len(dialogues) - self._summary_covers)
        for item in dialogues[-tail_records:] if tail_records else []:
            self._remember(
                prompts.build_user_prompt(item["question"], self.config.settings),
                item["answer"],
            )
        # Восстановленные сообщения уже отражены в блоке из файла: извлекатель их не переспрашивает.
        self._facts_covered = len(self._turns) // 2

    # --- внутреннее -----------------------------------------------------------------------

    def _route_memory(self, message: str) -> None:
        """Раскладывает реплику пользователя по слоям памяти правилами маршрутизации.

        Правило работает по оборотам реплики и не обращается к модели: маршрут детерминирован,
        не тратит токены и не зависит от ответа. Ответ модели источником записей не бывает —
        иначе в память попадали бы догадки модели, а не слова пользователя. Реплика без
        распознанных оборотов остаётся только в краткосрочном слое как ход диалога.
        """
        records = memory_layers.route(message)
        self._last_routing = records
        working_changed = False
        for record in records:
            if record.layer == memory_layers.WORKING:
                self._working[record.key] = record.value
                working_changed = True
            else:
                self.long_term.remember(record.key, record.value, record.category)
        if working_changed:
            self.history.set_working(self._working)

    def _store_working(self, key: str, value: str) -> None:
        self._working[key] = memory_layers.clip_value(value)
        self.history.set_working(self._working)

    def _next_note_key(self) -> str:
        """Свободный ключ для заметки: заметки не вытесняют друг друга, как ключи правил."""
        existing = {record.key for record in self.long_term.records()}
        index = 1
        while f"{memory_layers.CATEGORY_NOTE} {index}" in existing:
            index += 1
        return f"{memory_layers.CATEGORY_NOTE} {index}"

    def _signal(
        self,
        on_phase: Optional[Callable[[RequestPhase], None]],
        phase: RequestPhase,
    ) -> None:
        if on_phase is not None:
            on_phase(phase)

    def _prepare_context(
        self,
        question: str,
        user_prompt: str,
        on_phase: Optional[Callable[[RequestPhase], None]],
    ) -> int:
        """Готовит контекст перед вопросом и возвращает, сколько старейших обменов отброшено.

        Отброшенное — только вид запроса: потолок токенов может сузить окно стратегии, но
        ходы остаются в логе (см. `_shrink_to_ceiling`). Резюме обновляет суммаризатор,
        факты — извлекатель; обе памяти живут между вопросами.
        """
        strategy = self.config.strategy
        if strategy is ContextStrategy.STICKY_FACTS:
            self._update_facts(question, on_phase)
        if strategy is ContextStrategy.SUMMARY:
            self._digest_before_request(user_prompt, on_phase)
            return 0
        return self._shrink_to_ceiling(user_prompt)

    def _build_messages(self, user_prompt: str, skip: int = 0) -> List[Dict[str, str]]:
        """Сборка запроса: system настроек, профиль, инварианты, задача, память слоёв, память стратегии, ходы, новый ход."""
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": prompts.build_system_message(self.config.format)}
        ]
        profile = self._profile_message()
        if profile is not None:
            messages.append(profile)
        # Инварианты — сразу после профиля и выше задачи и памяти: жёсткие рамки стоят выше всего,
        # что модель могла бы принять за разрешение их обойти. Сообщение есть всегда.
        messages.append(self._invariants_message())
        task = self._task_message()
        if task is not None:
            messages.append(task)
        memory = self._memory_message()
        if memory is not None:
            messages.append(memory)
        strategy_memory = self._strategy_memory_message()
        if strategy_memory is not None:
            messages.append(strategy_memory)
        messages.extend(self._view_turns(skip))
        messages.append({"role": "user", "content": user_prompt})
        return messages

    def _profile_message(self) -> Optional[Dict[str, str]]:
        """Системное сообщение профиля персонализации: пустой профиль — None.

        Профиль идёт сразу после system настроек и выше памяти: он отвечает на «как отвечать»
        и не меняется от вопроса к вопросу, а память отвечает на «что известно». Инструкция
        ассета внутри сообщения подчиняет предпочтения профиля настройкам приложения.
        """
        content = profile_message(self.profile.active())
        if content is None:
            return None
        return {"role": "system", "content": content}

    @staticmethod
    def _invariants_message() -> Dict[str, str]:
        """Системное сообщение инвариантов агента: таблица правил и инструкция об отказе."""
        return {"role": "system", "content": invariants.invariants_message()}

    def _task_message(self) -> Optional[Dict[str, str]]:
        """Системное сообщение состояния задачи: пустая очередь — None.

        Стоит между профилем и слоями памяти: профиль отвечает на «как отвечать», состояние
        задачи — на «что мы сейчас делаем», память — на «что известно». Пустая очередь и очередь
        без незавершённых задач сообщения не дают, поэтому форма запроса без задачи не меняется.
        """
        content = task_state.task_message(self.task.state)
        if content is None:
            return None
        return {"role": "system", "content": content}

    def _memory_message(self) -> Optional[Dict[str, str]]:
        """Системное сообщение слоёв памяти: долговременная выше рабочей; пустые слои — None.

        Слои идут выше ходов диалога и выше памяти стратегии: свежая реплика пользователя
        остаётся последним сообщением, а инструкция ассета ставит её выше записей памяти.
        """
        records = memory_layers.records_from_block(
            memory_layers.WORKING, self._working
        ) + self.long_term.records()
        content = memory_layers.memory_message(records)
        if content is None:
            return None
        return {"role": "system", "content": content}

    def _strategy_memory_message(self) -> Optional[Dict[str, str]]:
        """Сообщение памяти активной стратегии: блок фактов или резюме (у веток и окна нет)."""
        strategy = self.config.strategy
        if strategy is ContextStrategy.STICKY_FACTS:
            if self._facts:
                return {"role": "system", "content": context_strategies.facts_message(self._facts)}
            return None
        if strategy is ContextStrategy.SUMMARY and self._summary:
            return {"role": "system", "content": context_compressor.summary_message(self._summary)}
        return None

    def _view_turns(self, skip: int = 0) -> List[Dict[str, str]]:
        """Ходы, которые активная стратегия отправляет в запрос (без обрезания по потолку).

        Окно и факты смотрят на весь лог, ветки — на ходы активной ветки, резюме — на хвост
        лога за пределами свёрнутого префикса. `skip` отбрасывает старейшие обмены вида при
        сокращении по потолку токенов.
        """
        strategy = self.config.strategy
        if strategy is ContextStrategy.BRANCHING:
            turns = self._branches.active_messages()
        elif strategy is ContextStrategy.SUMMARY:
            turns = self._turns[self._log_covered * 2 :]
        else:
            turns = list(self._turns)
        if strategy in (ContextStrategy.SLIDING_WINDOW, ContextStrategy.STICKY_FACTS):
            turns = context_strategies.window_messages(turns, self.config.settings.compress_after)
        return turns[skip * 2 :]

    def _shrink_to_ceiling(self, user_prompt: str) -> int:
        """Сужает вид стратегии, пока оценка запроса выше потолка токенов сессии.

        Отбрасываются старейшие обмены вида; последний обмен остаётся всегда — если после
        этого оценка всё ещё выше потолка, запрос уходит как есть (ходам ничего не грозит:
        они в логе, а не в запросе).
        """
        skip = 0
        while len(self._view_turns(skip)) > 2 and not self._fits_ceiling(user_prompt, skip):
            skip += 1
        return skip

    def _fits_ceiling(self, user_prompt: str, skip: int = 0) -> bool:
        """Приближённая оценка собираемого запроса не выше потолка токенов сессии."""
        return self._estimate(self._build_messages(user_prompt, skip)) <= (
            self.config.settings.max_session_tokens
        )

    def _context_tokens(self) -> int:
        """Оценка собираемого запроса без нового вопроса (для отчёта о контексте)."""
        return self._estimate(self._build_messages(""))

    @staticmethod
    def _estimate(messages: List[Dict[str, str]]) -> int:
        return sum(estimate_tokens(message["content"]) for message in messages)

    def _remember(self, user_content: str, assistant_content: str) -> None:
        self._turns.append({"role": "user", "content": user_content})
        self._turns.append({"role": "assistant", "content": assistant_content})
        self._branches.add_exchange()

    def _update_facts(
        self,
        question: str,
        on_phase: Optional[Callable[[RequestPhase], None]],
    ) -> None:
        """Обновляет блок фактов по сообщениям пользователя, ещё не отданным извлекателю.

        Очередь пополняется из лога: сообщения, отправленные при других стратегиях, тоже
        перерабатываются — иначе переключение на факты посреди диалога дало бы «слепой» блок.
        Извлекатель получает текущий блок и пакет сообщений (пакетами, если очередь не
        помещается в бюджет). Успешный ответ (в том числе пустой объект — «новых фактов нет»)
        снимает обработанные сообщения с очереди; сбой оставляет блок и очередь как были,
        чтобы ничего не потерять: они уйдут в следующее обновление. Ответ на вопрос от
        этого не зависит. Восстановленные из файла сообщения считаются переработанными.
        """
        if not self._facts_pending:
            self._facts_pending = self._logged_user_messages()
        self._facts_pending.append(question)
        answered_exchanges = len(self._turns) // 2
        while self._facts_pending:
            batch = context_strategies.facts_batch(
                self._facts, self._facts_pending, self.config.settings.max_session_tokens
            )
            self._signal(on_phase, RequestPhase.FACTS_UPDATE)
            try:
                meta = self.client.ask_with_usage_messages(
                    context_strategies.build_facts_messages(self._facts, batch),
                    max_tokens=config.max_tokens_for_words(config.FACTS_MAX_WORDS),
                    temperature=None,
                    model=self.config.model,
                )
            except APIError as exc:
                self._last_facts = FactsReport(
                    updated=False, keys=len(self._facts), error=str(exc)
                )
                return
            self._last_result = meta
            self._ledger.record(meta)
            parsed = context_strategies.parse_facts_response(meta.content)
            if parsed is None or not meta.content.strip():
                self._last_facts = FactsReport(
                    updated=False,
                    keys=len(self._facts),
                    error="Извлекатель фактов вернул пустой ответ или не JSON.",
                )
                return
            self._facts = context_strategies.merge_facts(self._facts, parsed)
            del self._facts_pending[: len(batch)]
            self.history.set_facts(self._facts)
            self._last_facts = FactsReport(updated=True, keys=len(self._facts))
        # Очередь разошлась целиком: всё, что в логе, переработано, плюс текущий вопрос —
        # он станет последним обменом после ответа. Сбой ответа ничего не пропустит:
        # провалившийся вопрос в диалог не попадает.
        self._facts_covered = answered_exchanges + 1

    def _logged_user_messages(self) -> List[str]:
        """Сообщения пользователя из лога, ещё не отданные извлекателю фактов."""
        answered = len(self._turns) // 2
        return [
            self._turns[index * 2]["content"]
            for index in range(self._facts_covered, answered)
        ]

    def _digest_before_request(
        self,
        user_prompt: str,
        on_phase: Optional[Callable[[RequestPhase], None]],
    ) -> None:
        """Сворачивает старые ходы в резюме перед отправкой вопроса (стратегия резюме).

        Два повода: оценка запроса выше потолка токенов (внеочередное сжатие) или длина
        несвёрнутого хвоста достигла порога сжатия (штатный). Резюме обновляется только
        после успешного суммаризатора, а свёрнутые ходы остаются в логе — растёт лишь счётчик
        покрытых обменов, поэтому переключение стратегии снова делает их отправимыми.
        """
        settings = self.config.settings
        digested_messages = 0
        digested_exchanges = 0
        last_meta: Optional[AnswerMeta] = None
        while True:
            uncovered = self._turns[self._log_covered * 2 :]
            if self._fits_ceiling(user_prompt) and len(uncovered) < settings.compress_after:
                break
            digest, _ = context_compressor.split_for_digest(uncovered)
            if not digest:
                break  # сжимать нечего: запрос уходит как есть, ходы не теряются
            batch = context_compressor.batch_within_budget(
                digest, self._summary, settings.max_session_tokens
            )
            self._signal(on_phase, RequestPhase.COMPRESSION)
            meta = self.client.ask_with_usage_messages(
                context_compressor.build_summary_messages(self._summary, batch),
                max_tokens=config.max_tokens_for_words(config.SUMMARY_MAX_WORDS),
                temperature=None,
                model=self.config.model,
            )
            self._last_result = meta
            self._ledger.record(meta)
            # Пустой дайджест — это сбой, а не резюме: принять его значит поднять счётчик
            # покрытых обменов, ничего не подставив в запрос, и бесследно выбросить ходы из
            # контекста. Запрос уже потрачен, поэтому расход учтён, а состояние памяти
            # остаётся прежним: следующий вопрос повторит сжатие.
            if not meta.content.strip():
                reason = (
                    " (finish_reason=length: модель израсходовала бюджет на рассуждение)"
                    if meta.finish_reason == "length"
                    else ""
                )
                raise APIError(
                    "Суммаризатор вернул пустой ответ — контекст не сжат" + reason + "."
                )
            self._summary = meta.content
            self._summary_covers += len(batch) // 2
            self._log_covered += len(batch) // 2
            self.history.set_summary(self._summary, self._summary_covers)
            digested_messages += len(batch)
            digested_exchanges += len(batch) // 2
            last_meta = meta
        if digested_exchanges:
            self._last_compression = CompressionReport(
                messages=digested_messages,
                exchanges=digested_exchanges,
                meta=last_meta,
            )

    @staticmethod
    def _usage_block(meta: AnswerMeta) -> Dict[str, Any]:
        """Метрики запроса для записи в history.json (cost_usd — None без цены)."""
        return {
            "prompt_tokens": meta.prompt_tokens,
            "completion_tokens": meta.completion_tokens,
            "total_tokens": meta.total_tokens,
            "cost_usd": meta.cost_usd,
        }


def _transition_entries(task: TaskItem) -> Tuple[TransitionEntry, ...]:
    """Журнал переходов задачи в ярлыках этапов — снимок для интерфейса."""
    return tuple(
        TransitionEntry(
            source=task_state.STAGE_LABELS[entry.source],
            target=task_state.STAGE_LABELS[entry.target],
            accepted=entry.accepted,
            reason=entry.reason,
        )
        for entry in task.transitions
    )


def _plan_mark(task: TaskItem, index: int) -> str:
    """Отметка подзадачи плана для снимка: выполнена, не выполнена или ещё не начата."""
    if index >= len(task.sections):
        return "◦"
    return "✗" if task.sections[index].failed else "✓"
