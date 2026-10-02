## 1. Цель в статус-баре

- [x] 1.1 Тесты `tests/unit/test_tui_app.py`: статус-бар показывает `Цель: «…»` после реплики с целью; без цели строки нет и раскладка прежняя; новая цель заменяет прежнюю; после `/clear` строки нет; разметка в тексте цели печатается как текст
- [x] 1.2 Реализовать часть цели в `ui/tui_app.py::_status_bar_line` через `agent.memory_report()` (запись с ключом `цель`), печать через `rich.markup.escape`; `pytest tests/unit/test_tui_app.py -q` — 191 passed
- [x] 1.3 Тест выявил дефект: реплика пользователя печаталась без `escape` — вопрос со «[/dim]» ронял отрисовку. Экранированы реплика (`Вы:`) и аргумент `/memory goal` — тот же класс, что чинили для профиля и `/memory remember`

## 2. Сквозной тест в PTY

- [x] 2.1 e2e `tests/e2e/test_memory_layers.py`: реплика с целью → строка `Цель:` на отрисованном экране; в последнем статус-баре после `/clear` строки нет; `pytest tests/e2e/test_memory_layers.py -q` — 9 passed

## 3. Документация

- [x] 3.1 Сверить `CLAUDE.md` и `README.md` с реальностью: строка цели в описании статус-бара, экранирование реплики пользователя; списки команд/модулей/переменных окружения без изменений

## 4. Закрытие изменения

- [x] 4.1 Полный `pytest -q` (с изоляцией `TABLETOP_RULES_INDEX_FILE`) зелёный (1362 passed); `openspec validate add-goal-status-line --strict` зелёный
- [x] 4.2 `openspec archive add-goal-status-line --yes`; `openspec validate --all --strict` зелёный
- [x] 4.3 Ветка влита в `main` через `git merge --ff-only`, тег `v0.25` запушен