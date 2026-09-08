## 1. Агент

- [x] 1.1 `TabletopAgent(client, history=None, ...)`: менеджер хранится в агенте, `restore_context()` без параметров читает собственную историю и вызывается в конце `__init__`; юнит-тесты: агент, созданный с непустой историей, шлёт первый вопрос с прошлым обменом; инъекция tmp-`HistoryManager` во всех тестах агента — `pytest tests/unit/test_tabletop_agent.py -q`
- [x] 1.2 `ask()` сразу пишет обмен в `self.history.add(question, answer)`; `reset()` чистит стек и файл; юнит-тесты: файл обновился сразу после ответа, ошибка API файл не меняет, `reset()` пустит обе памяти, прогон `/logictask` файл не трогает — `pytest tests/unit/test_tabletop_agent.py tests/unit/test_logictask*.py -q`

## 2. Интерфейс

- [x] 2.1 `TabletopAITUI`: проброс `history` в агента, реплей из `agent.history.dialogues`, `/clear` — только `agent.reset()`, убран `history.save()` при выходе; сигнатура конструктора сохранена; юнит-тесты TUI зелёные без изменения ассертов — `pytest tests/unit/test_tui_app.py tests/unit/test_history_manager.py -q`

## 3. E2E и документация

- [x] 3.1 Полный прогон `pytest` (e2e без правок — косвенное доказательство, что наблюдаемое поведение не изменилось); обновить `README.md`/`CLAUDE.md` (обе памяти — в агенте) — `pytest -q`
