## 1. Агент

- [x] 1.1 Добавить `TabletopAgent.restore_context(dialogues)`: пары «вопрос–ответ» → ходы user (через `build_user_prompt` с текущими настройками) и assistant (дословно), кап `HISTORY_LIMIT` обменов; юнит-тесты: засев трёх обменов, ходы уходят моделью, кап при переполнении, `reset()` после засева — `pytest tests/unit/test_tabletop_agent.py -q`
- [x] 1.2 Зафиксировать в тесте, что восстановленный user-ход собран инструкциями текущих настроек (формат/объём), а assistant-ход дословен — `pytest tests/unit/test_tabletop_agent.py -q`

## 2. Интерфейс

- [x] 2.1 Вызвать `agent.restore_context(history.dialogues)` в `TabletopAITUI` при старте (до реплея и первого ввода); юнит-тест: после конструирования с непустой историей первый вопрос уходит с прошлым обменом, `/clear` чистит файл и стек — `pytest tests/unit/test_tui_app.py tests/unit/test_history_manager.py -q`

## 3. E2E и документация

- [x] 3.1 Перевернуть e2e-тест контекста: два запуска приложения на общем `TABLETOP_HISTORY_FILE`, первый вопрос второго запуска содержит прошлый обмен; рестарт с пустой историей — без ходов; `/clear` после рестарта — без ходов — `pytest tests/e2e/test_agent_context.py -q`
- [x] 3.2 Полный прогон `pytest`; обновить `README.md`/`CLAUDE.md` (контекст агента переживает перезапуск, отклонение про инструкции текущих настроек) — `pytest -q`
