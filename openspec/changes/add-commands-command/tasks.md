## 1. Редьюсер панели команд

- [x] 1.1 Создать `tests/unit/test_commands_screen.py`: падающие тесты редьюсера — `initial_state` выбирает первую команду, ↑/↓ двигают выбор с зацикливанием, Enter/Esc фиксируют подтверждение/отмену, прочие клавиши игнорируются. Проверить: `pytest tests/unit/test_commands_screen.py` падает (модуля нет).
- [x] 1.2 Создать `ui/commands_screen.py`: `COMMAND_OPTIONS` (команда+описание для `/exit`, `/commands`, `/settings`, `/clear`, `/logictask`), `CommandsScreenState`, `initial_state()`, `apply_key()` по схеме `logictask_screen`. Проверить: `pytest tests/unit/test_commands_screen.py` зелёный.

## 2. Интеграция в tui_app

- [x] 2.1 `ui/tui_app.py`: диспетчер `/commands` → `_open_commands_screen()` (raw_mode + transient Live + цикл клавиш, рендер `_render_commands_panel`); при подтверждении выбранная команда выполняется тем же кодом, что и ручной набор, `/exit` — `_exit()` + сигнал остановки цикла. Падающие юнит-тесты в `tests/unit/test_tui_app.py`: выбор `/clear` очищает историю и печатает сообщение, выбор `/exit` завершает приложение, Esc — ничего не выполняет. Проверить: тесты зелёные.
- [x] 2.2 Статус-бар: константа `STATUS_COMMANDS = ["/exit", "/commands"]`, `_print_status_bar` печатает только её; `COMMANDS` выводится из `COMMAND_OPTIONS`. Юнит-тесты: подсказка содержит `/exit` и `/commands` и не содержит `/settings`, `/clear`, `/logictask`; `/commands` есть в автодополнении. Проверить: `pytest tests/unit` зелёный.

## 3. E2E

- [x] 3.1 Создать `tests/e2e/test_commands_flow.py`: `/commands` открывает панель со всеми командами и описаниями; стрелки двигают выбор; Enter выполняет выбранную команду (виден её эффект — например, «История диалога очищена.»); выбор `/settings` открывает экран настроек; Esc закрывает панель без действий; статус-бар показывает «Команды: /exit, /commands» и не показывает остальные. Паттерн ожиданий — как в `test_strategies.py` (ждать панель до стрелок, `wait_until_gone` после закрытия). Проверить: `pytest tests/e2e/test_commands_flow.py -q` зелёный.
- [x] 3.2 Перегнать снапшоты после смены статус-бара: `pytest --snapshot-update`, затем `pytest tests/e2e/test_screen_snapshots.py -q`. Проверить: диффы снапшотов — только строка «Команды:».

## 4. Финал

- [x] 4.1 Полный `pytest` зелёный (без маркера network).
- [x] 4.2 `openspec validate --changes` без ошибок.
