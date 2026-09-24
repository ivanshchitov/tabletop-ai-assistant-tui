## 1. Сервер: поиск с подробностями и признак ошибки

- [x] 1.1 `dnd_search` принимает `details` (boolean): с ним результат содержит основные поля найденных записей JSON-массивом после строки-заголовка (тест в `tests/unit/test_dnd_tools.py` против HTTP-заглушки)
- [x] 1.2 `dnd_server.py` ставит `isError` по признаку успеха `call_result` для всех инструментов; `MCPClient` получает `MCPError` (тест в `tests/unit/test_dnd_server.py`)

## 2. Сервер: сводка и сохранение

- [x] 2.1 `mcp_server/pipeline_tools.py`: `dnd_summarize(text, max_items)` — детерминированная сводка по JSON-записям или строкам-пунктам, ошибка на тексте без записей (`tests/unit/test_pipeline_tools.py`)
- [x] 2.2 `save_to_file(text, filename)` в каталог `TABLETOP_EXPORTS_DIR` (по умолчанию `exports/`): проверка имени, суффикс для занятого имени, отказ на пустой текст; путь и число символов в результате
- [x] 2.3 Сервер объявляет и диспетчеризует оба инструмента; `TABLETOP_EXPORTS_DIR` в `env_keys` записи `dnd-rules`; `exports/` в `.gitignore`; имена в тесте «без имён инструментов в core/ui»

## 3. Агент: цепочка

- [x] 3.1 `core/mcp_tools.parse_choice` разбирает `{"steps": [...]}` и прежний `{"tool": ...}` в кортеж шагов; `config.TOOL_CHAIN_MAX_STEPS = 4` (`tests/unit/test_mcp_tools.py`)
- [x] 3.2 Подстановка `$N` (`mcp_tools.resolve_references`): точное равенство, ссылка вперёд — ошибка
- [x] 3.3 `TabletopAgent` выполняет шаги по порядку, останавливается на сбое, держит `last_tool_chain`; `MCPToolResult` получает `step`, `total`, `sources`, `skipped` (`tests/unit/test_tabletop_agent.py`: шаг 2 получил ровно текст шага 1, один запрос выбора, остановка, потолок)
- [x] 3.4 Сообщение результата строится по всем успешным шагам, ссылка рендерится `← шаг N`; `assets/tool_choice_prompt.md` описывает цепочку и ссылки, `assets/tool_result_prompt.md` — несколько шагов

## 4. Терминал

- [x] 4.1 Строка журнала на шаг: `🔧 N/M инструмент (аргументы) ← шаг K (X симв.) → Y симв.`, неудачный шаг с причиной, отброшенные шаги (`tests/unit/test_tui_app.py`)

## 5. E2E

- [x] 5.1 `harness.AppSession` передаёт `TABLETOP_EXPORTS_DIR`; фейковый сервер записывает полученные аргументы
- [x] 5.2 `tests/e2e/test_tool_flow.py`: стаб отдаёт цепочку из трёх шагов на фейковый сервер — три строки журнала, второй вызов получил текст первого, запрос вопроса несёт результаты шагов, `history.json` без них
- [x] 5.3 Живой прогон против реального сервера `dnd-rules` (`tests/unit/test_dnd_server.py`): search(details) → summarize → save_to_file через `MCPClient` против HTTP-заглушки, файл во временном каталоге

## 6. Закрытие

- [x] 6.1 Полный `pytest -q` зелёный
- [x] 6.2 `openspec archive add-tool-pipeline --yes`, `openspec validate --all --strict`
- [x] 6.3 `CLAUDE.md`/`README.md`: сверить списки (инструменты сервера, переменные окружения, capabilities, test layout)
