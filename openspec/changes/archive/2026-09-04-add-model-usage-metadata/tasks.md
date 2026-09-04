## 1. Модели и цены

- [x] 1.1 Тест: `AVAILABLE_MODELS` содержит `minimax-m2.5` и `kimi-k3` в дополнение к пяти существующим (`tests/unit/test_config.py`) — красный, затем правка `core/config.py`, зелёный `pytest tests/unit/test_config.py`; коммит
- [x] 1.2 Тест: `MODEL_PRICING` покрывает каждую модель из `AVAILABLE_MODELS` парой цен (вход/выход за 1M) (`tests/unit/test_config.py`) — красный, затем добавление `MODEL_PRICING` в `core/config.py`, зелёный; коммит

## 2. Расчёт стоимости

- [x] 2.1 Тест: `core/usage.estimate_cost(model, prompt_tokens, completion_tokens)` считает стоимость по `MODEL_PRICING` для известной модели (`tests/unit/test_usage.py`, новый файл) — красный, затем `core/usage.py`, зелёный; коммит
- [x] 2.2 Тест: `estimate_cost` возвращает `None` для модели без цены в таблице, без исключения (`tests/unit/test_usage.py`) — красный, затем правка `estimate_cost`, зелёный; коммит

## 3. Метрики использования в API-клиенте

- [x] 3.1 Тест: `APIClient.ask_with_usage(...)` возвращает `AnswerMeta` с `content`, `model`, `elapsed_seconds` (> 0), `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost_usd` при моке HTTP-ответа с полем `usage` (`tests/unit/test_api_client.py`) — красный, затем `_request()` + `ask_with_usage()` в `core/api_client.py`, зелёный; коммит
- [x] 3.2 Тест: `APIClient.ask(...)` не меняет поведение (форма запроса, ретраи, обработка ошибок — существующие тесты `test_api_client.py` остаются зелёными без правок) — прогон `pytest tests/unit/test_api_client.py`, при необходимости точечная правка `ask()` на использование общего `_request()`, зелёный; коммит
- [x] 3.3 Тест: `ask_with_usage` для модели без цены в `MODEL_PRICING` возвращает `cost_usd is None`, без исключения (`tests/unit/test_api_client.py`) — красный, затем правка, зелёный; коммит

## 4. Доп.-информация после обычного вопроса

- [x] 4.1 Тест: заглушка `tests/e2e/stub_api.py` отдаёт `usage` (prompt_tokens/completion_tokens/total_tokens) в JSON-ответе — прогон существующих e2e, зелёный; коммит
- [x] 4.2 Тест: после ответа на вопрос на экране видна строка со временем/токенами/стоимостью (`tests/e2e/test_models_flow.py` или новый `tests/e2e/test_usage_metadata.py`) — красный, затем `_handle_question` в `ui/tui_app.py` переходит на `ask_with_usage` и печатает строку, зелёный; коммит
- [x] 4.3 Тест: `history.json` после вопроса не содержит доп.-информацию (только question/answer) (`tests/unit/test_tui_app.py` или e2e) — прогон, зелёный без правок (уже так по дизайну) или точечная правка; коммит

## 5. Доп.-информация в /logictask

- [x] 5.1 Тест: стратегия 1 («прямой ответ») печатает одну строку доп.-информации после ответа (`tests/e2e/test_strategies.py`) — красный, затем `_call`/`_run_strategy` в `ui/tui_app.py` переходят на `ask_with_usage` и печатают строку, зелёный; коммит
- [x] 5.2 Тест: стратегия 3 («промпт от модели») печатает две строки доп.-информации (после составленного промпта и после итогового ответа) (`tests/e2e/test_strategies.py`) — красный/зелёный; коммит
- [x] 5.3 Тест: стратегия 4 («панель экспертов») печатает три строки доп.-информации (по одной на эксперта) (`tests/e2e/test_strategies.py`) — красный/зелёный; коммит

## 6. Финал

- [x] 6.1 Полный `pytest` проекта зелёный; при расхождении — актуализировать `tests/e2e/snapshots/` через `pytest --snapshot-update` и проверить диффы вручную; коммит
- [x] 6.2 Обновить `CLAUDE.md` (список `AVAILABLE_MODELS`, новый `core/usage.py`, новый метод `ask_with_usage`, вывод доп.-информации) и `README.md` при необходимости; коммит
