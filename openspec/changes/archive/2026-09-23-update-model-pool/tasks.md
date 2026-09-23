## 1. Список моделей и цены

- [x] 1.1 Обновить `AVAILABLE_MODELS`, `DEFAULT_MODEL` и `MODEL_PRICING` в `core/config.py`;
      проверка — `tests/unit/test_config.py` и `tests/unit/test_usage.py` называют действующие модели
- [x] 1.2 Обновить тесты, называющие модели по имени (`test_models_screen.py`, `test_api_client.py`,
      `test_tabletop_agent.py`, `test_task_pipeline.py`, `test_tui_app.py`, `tests/e2e/test_models_flow.py`,
      `test_usage_metadata.py`, `test_live_api.py`, `cassettes.py`); проверка — `pytest tests/unit -q` зелёный
- [x] 1.3 Обновить снапшоты экрана со строкой статус-бара (`pytest --snapshot-update`) — осознанно,
      с проверкой диффа

## 2. Порог max_tokens

- [x] 2.1 Добавить `MIN_REQUEST_MAX_TOKENS` и порог в `max_tokens_for_words`; проверка — тесты
      формулы в `tests/unit/test_config.py` и ожидание потолка в `tests/e2e/test_settings_flow.py`

## 3. Проверка на живом API

- [x] 3.1 Прогнать `pytest tests/e2e/test_live_api.py -m network -q` — модель по умолчанию отвечает

## 4. Закрытие

- [x] 4.1 Полный `pytest` зелёный; `openspec validate update-model-pool --strict` зелёный
- [x] 4.2 Обновить `README.md` и `CLAUDE.md`: списки моделей, пояснения к тирам и к ценам
