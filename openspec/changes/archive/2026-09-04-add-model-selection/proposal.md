# Proposal: Выбор модели через команду /models

## Why

Эндпоинт OpenCode Zen обслуживает несколько моделей, но приложение жёстко зашито на `deepseek-v4-flash` (`core/config.py:MODEL_NAME`, единственное место использования — payload в `core/api_client.py`). Пользователь не может выбрать модель без правки кода. Заодно привязка к имени Deepseek разлезлась по именам классов, исключений и пользовательских сообщений об ошибках, хотя модель больше не единственная.

## What Changes

- Новая команда `/models`: интерактивная панель списка моделей (↑/↓ — выбор, Enter — применить, Esc — отмена без API-вызовов). Панель повторяет схему `/commands`: чистый редьюсер в `ui/models_screen.py`, цикл read-key/redraw в `ui/tui_app.py`.
- Фиксированный список моделей в `core/config.py`: `AVAILABLE_MODELS = [deepseek-v4-flash, deepseek-v4-pro, kimi-k2.5, glm-5.1, mimo-v2.5-free]`, `MODEL_NAME` переименована в `DEFAULT_MODEL` (первый элемент списка).
- Выбранная модель — сессионное состояние `TabletopAITUI` (по умолчанию — `DEFAULT_MODEL`), передаётся в каждый запрос параметром `client.ask(..., model=...)`; применяется и к вопросам, и к прогонам `/logictask`. Не персистится между запусками.
- Курсор панели `/models` стартует на текущей модели, текущая модель помечена; после закрытия панели статус-бар показывает активную модель.
- Рефакторинг нейтральных имён (поведение не меняется): `DeepseekAPIClient` → `APIClient`, `DeepseekAPIError` → `APIError`, тексты ошибок без имени Deepseek («Неверный API-ключ», «Ошибка API: …» и т.д.), обновление импортов/тестов/README.
- Панель `/commands` получает строку `/models` (единый источник — `COMMAND_OPTIONS`, автодополнение подхватывается само).

## Capabilities

### New Capabilities

- `model-selection`: команда `/models` — панель выбора модели из фиксированного списка, сессионное применение выбранной модели ко всем запросам (вопросы и `/logictask`), отображение активной модели в статус-баре.

### Modified Capabilities

- `api-integration`: форма запроса — вместо зашитой модели `deepseek-v4-flash` в payload уходит выбранная в сессии модель (по умолчанию `deepseek-v4-flash`); формулировка ошибки 401 и прочих сообщений нейтральна к имени модели.
- `configuration`: поведенческие константы — вместо единственной зашитой модели фиксированный список доступных моделей и модель по умолчанию из него.

## Impact

- `core/config.py` — `AVAILABLE_MODELS`, `DEFAULT_MODEL` (переименование `MODEL_NAME`).
- `core/api_client.py` — параметр `ask(..., model=...)`; рефакторинг имён класса/исключения/сообщений.
- `ui/models_screen.py` — новый редьюсер; `ui/tui_app.py` — команда `/models`, состояние `self.model`, статус-бар, импорты.
- `ui/commands_screen.py` — строка `/models` в `COMMAND_OPTIONS`.
- Тесты: `tests/unit/test_models_screen.py` (новый), `test_api_client.py`, `test_tui_app.py`, `test_config.py`, `tests/e2e/test_commands_flow.py` или новый e2e-файл, stub-сервер уже записывает payload.
- `README.md` — имя клиента, описание выбора модели.
