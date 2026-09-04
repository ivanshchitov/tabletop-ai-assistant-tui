## 1. Таймаут запроса

- [x] 1.1 Тест: `config.REQUEST_TIMEOUT == 90`, когда `TABLETOP_REQUEST_TIMEOUT` не задана (`tests/unit/test_config.py`) — красный, затем правка `core/config.py`, зелёный
- [x] 1.2 Прогон `tests/e2e/harness.py` и `tests/e2e/test_session.py`: явное переопределение `TABLETOP_REQUEST_TIMEOUT` в тестах продолжает работать (env всегда приоритетнее дефолта) — зелёный без правок

## 2. Документация

- [x] 2.1 Обновить `README.md`/`CLAUDE.md`, если там упомянуто значение 30 секунд для `TABLETOP_REQUEST_TIMEOUT`
- [x] 2.2 Полный `pytest` проекта зелёный

## 3. Финал

- [x] 3.1 `/opsx:archive` — синк delta-спеки (`configuration`) в основную, архивация
