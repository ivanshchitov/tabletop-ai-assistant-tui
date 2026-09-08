"""Агент Tabletop AI Assistant: стек сообщений сессии и пересылка в LLM.

Агент — единственное место, где решается, что и как отправляется модели: он владеет
стеком сообщений текущей сессии (ходы user/assistant успешных обменов), пересобирает
системное сообщение из актуальных настроек при каждом запросе и решает фиксированную
логическую задачу выбранной стратегией промптинга. Терминальный интерфейс только
показывает результат: агент ничего не печатает и ошибки API отдаёт исключением.
"""

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from . import config, logictask, prompts
from .answer_settings import AnswerFormat, AnswerSettings
from .api_client import APIClient, AnswerMeta
from .history_manager import HistoryManager

# Метка промежуточного результата стратегии 3 (составленный моделью промпт).
COMPOSED_PROMPT_LABEL = "Составленный моделью промпт"

@dataclass
class AgentConfig:
    """Единый конфиг агента: настройки ответа сессии и модель.

    Настройки ответа живут в AnswerSettings — валидация (без тихого клампинга) остаётся
    в его with_*-методах; модель — отдельное поле, её меняет экран /models.
    """

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
    """Вопрос -> сборка сообщений -> LLM -> ответ с метриками."""

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
        # Ходы user/assistant успешных обменов; system в стеке не хранится (пересобирается).
        self._turns: List[Dict[str, str]] = []
        self._last_result: Optional[AnswerMeta] = None
        # Обе памяти агента — свои: стек сессии восстанавливается из файла истории
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

    def ask(self, question: str) -> AnswerMeta:
        """Задаёт вопрос с контекстом сессии; при ошибке API поднимает APIError."""
        user_prompt = prompts.build_user_prompt(question, self.config.settings)
        meta = self.client.ask_with_usage_messages(
            self._build_messages(user_prompt),
            max_tokens=config.max_tokens_for_words(self.config.max_words),
            temperature=self.config.temperature,
            model=self.config.model,
        )
        self._last_result = meta
        self._remember(user_prompt, meta.content)
        # Долговременная память: пара «вопрос–ответ» — на диск сразу после ответа.
        self.history.add(question, meta.content)
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
        """Опустошает стек сообщений и файл истории (команда /clear)."""
        self._turns.clear()
        self.history.clear()

    def restore_context(self) -> None:
        """Засеивает стек ходами из собственной истории агента (пары «вопрос–ответ»).

        user-ход собирается инструкциями текущих настроек — в истории хранится вопрос,
        а не собранный промпт; ответ кладётся дословно. Потолок глубины тот же, что
        у живой сессии (кап в _remember). Вызывается конструктором автоматически.
        """
        for item in self.history.dialogues:
            self._remember(
                prompts.build_user_prompt(item["question"], self.config.settings),
                item["answer"],
            )

    # --- внутреннее -----------------------------------------------------------------------

    def _build_messages(self, user_prompt: str) -> List[Dict[str, str]]:
        """system из текущих настроек + ходы сессии + новый user-ход."""
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": prompts.build_system_message(self.config.format)}
        ]
        messages.extend(self._turns)
        messages.append({"role": "user", "content": user_prompt})
        return messages

    def _remember(self, user_content: str, assistant_content: str) -> None:
        self._turns.append({"role": "user", "content": user_content})
        self._turns.append({"role": "assistant", "content": assistant_content})
        cap = config.HISTORY_LIMIT * 2
        if len(self._turns) > cap:
            del self._turns[: len(self._turns) - cap]

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
        return meta
