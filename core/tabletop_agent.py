"""Агент Tabletop AI Assistant: память диалога, стратегии контекста и пересылка в LLM.

Агент — единственное место, где решается, что и как отправляется модели. Он владеет всей
памятью диалога: логом ходов сессии (он только растёт — ходы из него не удаляются), сжатым
резюме, блоком фактов, ветками и файлом истории, — и собирает запрос по активной стратегии
управления контекстом. Стратегия — это вид на лог, а не его порча: переключение стратегии
ничего не теряет. Терминальный интерфейс только показывает результат и получает от агента
снимок состояния контекста: агент ничего не печатает, ошибки API отдаёт исключением.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config, context_compressor, context_strategies, memory_layers, prompts
from .usage import SessionLedger, SessionUsage, estimate_tokens
from .answer_settings import AnswerFormat, AnswerSettings, ContextStrategy
from .api_client import APIClient, AnswerMeta, APIError
from .history_manager import HistoryManager
from .long_term_memory import LongTermMemory


class RequestPhase(Enum):
    """Фаза запроса агента: вспомогательный запрос стратегии или сам вопрос.

    Агент не знает ни rich, ни слов отображения: слушатель фаз получает перечисление,
    терминальный слой маппит его на подписи индикатора.
    """

    COMPRESSION = "compression"
    FACTS_UPDATE = "facts_update"
    REQUEST = "request"


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
        meta = self.client.ask_with_usage_messages(
            self._build_messages(user_prompt, skip),
            max_tokens=config.max_tokens_for_words(self.config.max_words),
            temperature=self.config.temperature,
            model=self.config.model,
        )
        self._last_result = meta
        self._ledger.record(meta)
        self._remember(user_prompt, meta.content)
        # Долговременная память: пара «вопрос–ответ» с метриками — на диск сразу после ответа.
        self.history.add(question, meta.content, usage=self._usage_block(meta))
        return meta

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
        между сессиями: она остаётся и в памяти, и в своём файле, снять её можно только явно
        командой `/memory forget all`.
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
        """Сборка запроса: system настроек, память слоёв, память стратегии, ходы, новый user-ход."""
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": prompts.build_system_message(self.config.format)}
        ]
        memory = self._memory_message()
        if memory is not None:
            messages.append(memory)
        strategy_memory = self._strategy_memory_message()
        if strategy_memory is not None:
            messages.append(strategy_memory)
        messages.extend(self._view_turns(skip))
        messages.append({"role": "user", "content": user_prompt})
        return messages

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
