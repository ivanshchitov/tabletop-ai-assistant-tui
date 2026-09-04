## 1. Конфигурация и клиент

- [x] 1.1 `core/config.py`: `AVAILABLE_MODELS` (5 моделей), `DEFAULT_MODEL = AVAILABLE_MODELS[0]`, переименование `MODEL_NAME` → `DEFAULT_MODEL`; обновить `tests/unit/test_config.py` (список, дефолт). Коммит.
- [x] 1.2 `core/api_client.py`: параметр `ask(..., model=config.DEFAULT_MODEL)` в payload; тест в `tests/unit/test_api_client.py` — payload["model"] равен переданному и дефолту. Коммит.

## 2. Редьюсер панели /models

- [x] 2.1 `ui/models_screen.py`: `ModelSelectionState`, `initial_state(current_model)` (курсор на текущей), `apply_key` (↑/↓, Enter→confirmed, Esc→cancelled); тесты `tests/unit/test_models_screen.py`: старт на текущей, цикличность ↑/↓, Enter, Esc, игнор прочих клавиш. Коммит.

## 3. Врезка в приложение

- [x] 3.1 `ui/commands_screen.py`: строка `("/models", "выбрать модель для ответов")` в `COMMAND_OPTIONS`; тест автодополнения/панели видит `/models`. Коммит.
- [x] 3.2 `ui/tui_app.py`: `self.model`, `_handle_command("/models")` → панель (raw_mode+Live, рендер с пометкой текущей модели), Enter меняет `self.model`, Esc — нет; модель в статус-баре; вопросы и `/logictask` передают `model=self.model`; юнит-тесты `tests/unit/test_tui_app.py` (смена модели, Esc не меняет, logictask несёт модель, статус-бар). Коммит.

## 4. E2E

- [x] 4.1 `tests/e2e`: сценарий через pty+stub: `/models` → курсор на текущей → ↓/Enter → вопрос → stub получил новую модель в payload; Esc-ветка — модель не изменилась. Коммит.
- [x] 4.2 Прогнать `pytest tests/e2e` целиком; при съехавших снапшотах статус-бара — `pytest --snapshot-update` и ревью диффа. Коммит.

## 5. Рефакторинг нейтральных имён

- [x] 5.1 `core/api_client.py`: `DeepseekAPIClient`→`APIClient`, `DeepseekAPIError`→`APIError`, тексты ошибок без Deepseek; обновить импорты/строки в `tests/unit/test_api_client.py`, `test_tui_app.py`, `tests/e2e/test_session.py`, `test_recorded_answers.py`, `README.md:49`; `pytest` зелёный. Коммит.

## 6. Финал

- [x] 6.1 Полный `pytest` (без `-m network`) зелёный; `openspec validate add-model-selection` без ошибок. Коммит.
- [x] 6.2 `/opsx-archive` и merge ветки в `main`.
