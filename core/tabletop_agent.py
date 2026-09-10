"""Агент Tabletop AI Assistant: стек сообщений сессии, сжатие истории и пересылка в LLM.

Агент — единственное место, где решается, что и как отправляется модели: он владеет
стеком сообщений текущей сессии, сжатым резюме более старых обменов и файлом истории,
пересобирает системное сообщение из актуальных настроек при каждом запросе и решает
фиксированную логическую задачу выбранной стратегией промптинга. Терминальный интерфейс
только показывает результат: агент ничего не печатает и ошибки API отдаёт исключением.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import config, context_compressor, logictask, prompts
from .usage import SessionLedger, SessionUsage, estimate_tokens
from .answer_settings import AnswerFormat, AnswerSettings
from .api_client import APIClient, AnswerMeta
from .history_manager import HistoryManager

# Метка промежуточного результата стратегии 3 (составленный моделью промпт).
COMPOSED_PROMPT_LABEL = "Составленный моделью промпт"


class RequestPhase(Enum):
    """Фаза запроса агента: сжатие контекста перед вопросом или сам вопрос.

    Агент не знает ни rich, ни слов отображения: слушатель фаз получает перечисление,
    терминальный слой маппит его на подписи индикатора («Суммаризация...» / «Отправка...»).
    """

    COMPRESSION = "compression"
    REQUEST = "request"


@dataclass
class CompressionReport:
    """Что свернул последний суммаризатор: размер пакета и метрики его запроса."""

    messages: int
    exchanges: int
    meta: AnswerMeta


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
    def max_words(self) -> int:
        return self.settings.max_words

    @property
    def list_limit(self) -> int:
        return self.settings.list_limit

    @property
    def temperature(self) -> float:
        return self.settings.temperature


class TabletopAgent:
    """Вопрос -> сжатие контекста -> сборка сообщений -> LLM -> ответ с метриками."""

    def __init__(
        self,
        client: Optional[APIClient],
        settings: Optional[AnswerSettings] = None,
        model: Optional[str] = None,
        history: Optional[HistoryManager] = None,
    ) -> None:
        self.client = client
        self.config = AgentConfig(
            settings=settings if settings is not None else AnswerSettings(),
            model=model if model is not None else config.DEFAULT_MODEL,
        )
        self.history = history if history is not None else HistoryManager()
        # Неотжатые ходы user/assistant успешных обменов; system в стеке не хранится.
        self._turns: List[Dict[str, str]] = []
        # Третья память агента: сжатое резюме и число покрытых им ведущих обменов файла.
        self._summary: Optional[str] = None
        self._summary_covers: int = 0
        self._last_compression: Optional[CompressionReport] = None
        self._last_result: Optional[AnswerMeta] = None
        # Учёт расхода сессии: каждый успешный запрос к API (вопрос, /logictask, суммаризатор).
        self._ledger = SessionLedger()
        # Обе памяти агента — свои: контекст восстанавливается из файла истории
        # сразу при создании, «забыть вызвать» восстановление невозможно.
        self.restore_context()

    @property
    def settings(self) -> AnswerSettings:
        """Настройки ответа (формат/объём/лимит списка/температура) внутри конфига агента."""
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
    def session_usage(self) -> SessionUsage:
        """Итоги сессии: накопленные токены и стоимость всех успешных запросов."""
        return self._ledger.usage

    @property
    def stack_exchanges(self) -> int:
        """Число неотжатых обменов в стеке сессии (пары user/assistant)."""
        return len(self._turns) // 2

    @property
    def stack_tokens_estimate(self) -> int:
        """Приближённая оценка токенов собираемого запроса (system + резюме + ходы).

        Клиентская эвристика, не реальное число токенов запроса — эталон виден в
        prompt_tokens последнего ответа. Резюме входит в оценку.
        """
        total = estimate_tokens(prompts.build_system_message(self.config.format))
        if self._summary:
            total += estimate_tokens(context_compressor.summary_message(self._summary))
        total += sum(estimate_tokens(turn["content"]) for turn in self._turns)
        return total

    def ask(
        self,
        question: str,
        on_phase: Optional[Callable[[RequestPhase], None]] = None,
    ) -> AnswerMeta:
        """Задаёт вопрос с контекстом сессии; при ошибке API поднимает APIError.

        Сжатие выполняется перед вопросом: когда оценка собираемого запроса выше
        потолка токенов сессии или неотжатых сообщений достигло порога сжатия,
        суммаризатор сворачивает старейшие ходы (кроме последнего обмена) в резюме.
        """
        user_prompt = prompts.build_user_prompt(question, self.config.settings)
        self._digest_before_request(user_prompt, on_phase)
        self._signal(on_phase, RequestPhase.REQUEST)
        meta = self.client.ask_with_usage_messages(
            self._build_messages(user_prompt),
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

    def solve_logictask(self, strategy_number: int) -> Iterator[Tuple[Optional[str], AnswerMeta]]:
        """Прогон логической задачи выбранной стратегией, вне стека контекста.

        Отдаёт результаты по мере выполнения — по одному на каждый запрос: метка None
        или текст этапа (промпт стратегии 3, роль эксперта стратегии 4). Настройки
        ответа сессии к прогонам не применяются: без температуры (клиентский дефолт)
        и с max_tokens от объёма по умолчанию. Ошибка API прерывает остаток прогона
        исключением.
        """
        if strategy_number == 1:
            yield None, self._logictask_call(*logictask.build_direct_prompts())
        elif strategy_number == 2:
            yield None, self._logictask_call(*logictask.build_stepwise_prompts())
        elif strategy_number == 3:
            composed = self._logictask_call(*logictask.build_prompt_compose_prompts())
            yield COMPOSED_PROMPT_LABEL, composed
            yield None, self._logictask_call(*logictask.build_solve_with_prompt_prompts(composed.content))
        else:
            for role in logictask.EXPERT_ROLES:
                yield role, self._logictask_call(*logictask.build_expert_prompts(role))

    def reset(self) -> None:
        """Опустошает стек, резюме, файл истории и накопитель сессии (команда /clear)."""
        self._turns.clear()
        self._summary = None
        self._summary_covers = 0
        self._last_compression = None
        self.history.clear()
        self._ledger.reset()

    def restore_context(self) -> None:
        """Засеивает контекст из собственной истории агента: резюме + неотжатый хвост.

        Резюме из конверта занимает своё место в собираемом запросе; сообщения обменов,
        не покрытых резюме, становятся неотжатыми ходами стека (пары user/assistant) —
        ход user собирается инструкциями активных настроек, ход assistant дословно.
        Обмены, не покрытые резюме, догоняются суммаризатором по частям при первом
        вопросе — тем же механизмом, что и живая сессия, без отдельного кода рестарта.
        """
        self._summary = self.history.summary
        self._summary_covers = self.history.summary_covers
        dialogues = self.history.dialogues
        tail_records = max(0, len(dialogues) - self._summary_covers)
        for item in dialogues[-tail_records:] if tail_records else []:
            self._remember(
                prompts.build_user_prompt(item["question"], self.config.settings),
                item["answer"],
            )

    # --- внутреннее -----------------------------------------------------------------------

    def _signal(
        self,
        on_phase: Optional[Callable[[RequestPhase], None]],
        phase: RequestPhase,
    ) -> None:
        if on_phase is not None:
            on_phase(phase)

    def _build_messages(self, user_prompt: str) -> List[Dict[str, str]]:
        """system из текущих настроек + резюме (если есть) + неотжатые ходы + новый user-ход."""
        system = prompts.build_system_message(self.config.format)
        messages = context_compressor.context_messages(system, self._summary, self._turns)
        messages.append({"role": "user", "content": user_prompt})
        return messages

    def _remember(self, user_content: str, assistant_content: str) -> None:
        self._turns.append({"role": "user", "content": user_content})
        self._turns.append({"role": "assistant", "content": assistant_content})

    def _fits_ceiling(self, user_prompt: str) -> bool:
        """Приближённая оценка собираемого запроса не выше потолка токенов сессии."""
        system = prompts.build_system_message(self.config.format)
        return (
            context_compressor.estimate_context_tokens(
                system, self._summary, self._turns, user_prompt
            )
            <= self.config.settings.max_session_tokens
        )

    def _digest_before_request(
        self,
        user_prompt: str,
        on_phase: Optional[Callable[[RequestPhase], None]],
    ) -> None:
        """Сворачивает старые ходы в резюме перед отправкой вопроса.

        Два повода: оценка запроса выше потолка токенов (внеочередное сжатие) или
        неотжатых сообщений достиг порог сжатия (штатный). Ходы удаляются из стека
        только после успешного суммаризатора; потолок, который не уходит после
        сворачивания всего, кроме последнего обмена, не исполним — запрос идёт как есть.
        """
        settings = self.config.settings
        digested_messages = 0
        digested_exchanges = 0
        last_meta: Optional[AnswerMeta] = None
        while True:
            if self._fits_ceiling(user_prompt) and len(self._turns) < settings.compress_after:
                break
            digest, _ = context_compressor.split_for_digest(self._turns)
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
            self._summary = meta.content
            self._summary_covers += len(batch) // 2
            self.history.set_summary(self._summary, self._summary_covers)
            del self._turns[: len(batch)]
            digested_messages += len(batch)
            digested_exchanges += len(batch) // 2
            last_meta = meta
        if digested_exchanges:
            self._last_compression = CompressionReport(
                messages=digested_messages,
                exchanges=digested_exchanges,
                meta=last_meta,
            )

    def _logictask_call(self, system: str, user: str) -> AnswerMeta:
        # Потолок токенов фиксируется от объёма по умолчанию; температура не передаётся —
        # /logictask живёт на клиентском дефолте.
        max_tokens = config.max_tokens_for_words(config.DEFAULT_MAX_WORDS)
        meta = self.client.ask_with_usage_messages(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            model=self.config.model,
        )
        self._last_result = meta
        self._ledger.record(meta)
        return meta

    @staticmethod
    def _usage_block(meta: AnswerMeta) -> Dict[str, Any]:
        """Метрики запроса для записи в history.json (cost_usd — None без цены)."""
        return {
            "prompt_tokens": meta.prompt_tokens,
            "completion_tokens": meta.completion_tokens,
            "total_tokens": meta.total_tokens,
            "cost_usd": meta.cost_usd,
        }
