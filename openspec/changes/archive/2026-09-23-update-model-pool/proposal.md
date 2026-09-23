## Why

Провайдер OpenCode Zen вывел из обращения часть моделей: запрос с моделью по умолчанию
`deepseek-v4-flash` отвечает `HTTP 403` («Model access is disabled»), а список
`/zen/v1/models` больше не содержит ни её, ни `kimi-k2.5`, ни `kimi-k2.6`, ни `glm-5.1`.
Приложение в таком виде не отвечает на вопросы вовсе: сломана именно модель по умолчанию.

## What Changes

- Модель по умолчанию `deepseek-v4-flash` заменяется на её действующего преемника
  `deepseek-v4.1-flash`.
- `glm-5.1` заменяется на `glm-5.3-flash`.
- `kimi-k2.5` и `kimi-k2.6` удаляются из списка: у провайдера их больше нет, а тир сравнения
  закрывают оставшиеся модели.
- Таблица цен приводится в соответствие с действующим прайсом провайдера для моделей списка.
- `max_tokens` получает нижний порог: новая модель по умолчанию тратит сотни токенов рассуждения
  до первого символа и при тесном потолке (например, 170 токенов на 30-словный ответ) возвращает
  пустой ответ.

## Capabilities

### New Capabilities
<!-- нет -->

### Modified Capabilities
- `model-selection`: список моделей панели `/models`, модель по умолчанию и таблица цен
  называют действующие модели провайдера.
- `configuration`: константа списка доступных моделей и модель по умолчанию.
- `api-integration`: модель запроса по умолчанию и нижний порог `max_tokens`.

## Impact

- `core/config.py` (`AVAILABLE_MODELS`, `DEFAULT_MODEL`, `MODEL_PRICING`, `MIN_REQUEST_MAX_TOKENS`).
- Тесты, называющие модели: `tests/unit/test_config.py`, `test_models_screen.py`, `test_usage.py`,
  `test_api_client.py`, `test_tabletop_agent.py`, `test_task_pipeline.py`, `test_tui_app.py`,
  `tests/e2e/test_models_flow.py`, `test_usage_metadata.py`, `test_live_api.py`, `cassettes.py`
  и снапшоты экрана со строкой статус-бара.
- `README.md`, `CLAUDE.md` — списки моделей и пояснения к ним.
