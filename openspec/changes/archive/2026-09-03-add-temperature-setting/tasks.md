## 1. Ядро: настройка и конфиг

- [x] 1.1 Падающие unit-тесты `tests/unit/test_answer_settings.py`: температура по умолчанию 0.7; `with_temperature` принимает 0.0/1.2/2.0; отклоняет 2.5, -0.1 и 0.55/1.15 (инвариант одного знака после точки для программных вызовов); снимок неизменяем, независимое применение полей. Запуск: `pytest tests/unit/test_answer_settings.py`
- [x] 1.2 `core/config.py`: `MIN_TEMPERATURE = 0.0`, `MAX_TEMPERATURE = 2.0`; `core/answer_settings.py`: поле `temperature` и `with_temperature()` (диапазон + `round(value, 1) != value` → ошибка). Запуск: `pytest tests/unit/test_answer_settings.py tests/unit/test_config.py`

## 2. Клиент API и логиктаск

- [x] 2.1 Падающие unit-тесты `tests/unit/test_api_client.py`: `ask()` принимает `temperature` и кладёт его в payload; дефолт — `config.TEMPERATURE`. Запуск: `pytest tests/unit/test_api_client.py`
- [x] 2.2 `core/api_client.py`: параметр `temperature: float = config.TEMPERATURE` у `ask()`. Падающий unit-тест `tests/unit/test_logictask.py` (или существующий прогон стратегий): запросы прогона идут с температурой по умолчанию даже при изменённой настройке сессии. Запуск: `pytest tests/unit/test_api_client.py tests/unit/test_logictask.py`

## 3. Экран /settings (редьюсер)

- [x] 3.1 Падающие unit-тесты `tests/unit/test_settings_screen.py`: четвёртая строка; навигация по четырём строкам с циклом; на строке температуры цифры и первая точка дописываются, но не более одной цифры после точки — второй знак не вводится (0.5 + «5» → 0.5), вторая точка и нечисловые игнорируются, Backspace стирает; парсинг на выходе `^\d+(\.\d)?$`; «0.» → ошибка про температуру, остальные поля применяются; `initial_state` подхватывает 0.7 → "0.7". Запуск: `pytest tests/unit/test_settings_screen.py`
- [x] 3.2 `ui/settings_screen.py`: `ROW_TEMPERATURE`, ограничение ввода (одна точка, одна цифра после точки), парсинг и валидация в `apply_to_settings`. Запуск: `pytest tests/unit/test_settings_screen.py`

## 4. Главный цикл и отрисовка

- [x] 4.1 Падающие unit-тесты `tests/unit/test_tui_app.py`: панель настроек показывает строку «Температура» с диапазоном; статус-бар содержит температуру; вопрос уходит в клиент с `self.settings.temperature`. Запуск: `pytest tests/unit/test_tui_app.py`
- [x] 4.2 `ui/tui_app.py`: отрисовка четвёртой строки, температура в статус-баре, передача температуры в `ask()`. Запуск: `pytest tests/unit/test_tui_app.py`

## 5. E2E и полный прогон

- [x] 5.1 Падающие e2e-тесты `tests/e2e/test_settings_flow.py` и/или `tests/e2e/test_session.py`: смена температуры на экране видна в статус-баре; stub-сервер фиксирует `temperature` в payload. Запуск: `pytest tests/e2e -q`
- [x] 5.2 При изменении отрисовки панели — обновить снапшоты (`pytest --snapshot-update`) и убедиться, что изменение осознанное. Запуск: `pytest tests/e2e/test_screen_snapshots.py -q`
- [x] 5.3 Полный `pytest` зелёный (unit + e2e, без `network`). Запуск: `pytest -q`
