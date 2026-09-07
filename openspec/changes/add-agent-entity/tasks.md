## 1. Клиент и агент

- [ ] 1.1 Перевести `APIClient._request()` на массив `messages=[...]` (пара `system/user` — частный случай поверх общего пути); существующие юнит-тесты клиента зелёные — `pytest tests/unit/test_api_client.py -q`, коммит
- [ ] 1.2 Создать `core/tabletop_agent.py`: `TabletopAgent` со стеком сообщений, `ask()` (system пересобирается каждый раз, кап `HISTORY_LIMIT`), `reset()`, ошибки — исключением; юнит-тесты: второй запрос несёт первый ход, кап 50, пересборка system при смене формата, прошлые user-ходы не переписываются — `pytest tests/unit/test_tabletop_agent.py -q`, коммит
- [ ] 1.3 Перенести оркестрацию `/logictask` в `TabletopAgent.solve_logictask(strategy)` (1/1/2/3 запроса, параметры прогонов, yield `(label, AnswerMeta)` по мере выполнения, стек не меняется); юнит-тесты: составы стратегий, стек идентичен до/после — `pytest tests/unit -q`, коммит

## 2. Интерфейс

- [ ] 2.1 Переключить `TabletopAITUI` на агента: `_handle_question` делегирует `agent.ask()` (спиннер, проверка JSON, история, счётчик — в UI); сигнатура конструктора `TabletopAITUI(console=, history=, client=)` сохранена — `pytest tests/unit/test_tui_app.py -q`, коммит
- [ ] 2.2 Заменить `_run_strategy` в UI на потребление `agent.solve_logictask()` (печать по мере прихода, включая промежуточный промпт стратегии 3), удалить дубль `_call` — `pytest tests/unit/test_logictask*.py -q`, коммит
- [ ] 2.3 Подключить `/clear` к `agent.reset()` (стек и история опустошаются вместе) — `pytest tests/unit/test_history_manager.py tests/unit/test_tui_app.py -q`, коммит

## 3. E2E и документация

- [ ] 3.1 Обновить e2e-ассерты: второй запрос содержит первый ход (user+assistant), запрос после `/clear` содержит один user-ход, вопрос после `/logictask` идёт в неизменном составе сообщений, ход за капом не уходит — `pytest tests/e2e -q`, коммит
- [ ] 3.2 Полный прогон `pytest` и обновить `README.md`/`CLAUDE.md` (архитектура: агент как отдельный слой `core/tabletop_agent.py`) — `pytest -q`, коммит
