# Tasks

## 1. Ядро: накопитель и оценка

- [x] 1.1 `core/config.py`: `ESTIMATED_CHARS_PER_TOKEN = 3`; `HISTORY_LIMIT` из
      `TABLETOP_HISTORY_LIMIT` (по умолчанию 50)
- [x] 1.2 `core/usage.py`: `estimate_tokens(text)`; `SessionLedger` (`record`, `reset`, снимок:
      запросы, входные/выходные/общие токены, стоимость, средний запрос); стоимость сессии None,
      если стоимость хоть одного запроса неизвестна; суммирование метрик списка записей истории
      (`sum_usage(records)` для итога «всего диалога»: записи без `usage` пропускаются, стоимость
      None, если стоимость хоть одной записи с метриками неизвестна)
- [x] 1.3 Тесты: `tests/unit/test_usage.py` (оценка, накопление, сброс, None-стоимость,
      суммирование записей); `tests/unit/test_config.py` (env-override лимита)

## 2. Клиент, агент и память

- [x] 2.1 `core/api_client.py`: `AnswerMeta.finish_reason` из `choices[0].finish_reason`
      (None при отсутствии); все конструкторы метаданных обновлены
- [x] 2.2 `core/tabletop_agent.py`: агент владеет `SessionLedger`; запись метрик каждого
      успешного запроса (вопрос и стратегии `/logictask`); `reset()` сбрасывает накопитель;
      свойства `session_usage` и оценка токенов стека (системное сообщение + ходы) через
      `estimate_tokens`; расход «всего диалога» агент берёт у менеджера истории
- [x] 2.3 `core/history_manager.py`: `add(question, answer, usage=None)` — запись с блоком
      `usage` или прежнего вида без метрик; суммирование по записям; старые записи и файлы
      читаются как раньше
- [x] 2.4 Тесты: `tests/unit/test_api_client.py` (finish_reason есть/нет);
      `tests/unit/test_tabletop_agent.py` (накопление по вопросам и `/logictask`, сброс в
      `reset()`, оценка стека растёт с ходами и обрезается окном);
      `tests/unit/test_history_manager.py` (запись с метриками и без; чтение файла старого
      формата; суммирование; вытеснение уносит расход)

## 3. Интерфейс

- [x] 3.1 `ui/tui_app.py`: предупреждения об усечении (пустой `content` + `length`; непустой
      `content` + `length`) над строкой метрик в пути вопроса
- [x] 3.2 `ui/tui_app.py` + `ui/commands_screen.py`: команда `/usage` в `COMMAND_OPTIONS`,
      обработчик с отчётом (последний запрос / итоги сессии / расход сохранённой истории /
      окно контекста, «≈» для оценки)
- [x] 3.3 `ui/tui_app.py::_print_status_bar`: итоги сессии в статус-баре (суммарные токены и
      стоимость текущей сессии, «неизвестно» при неопределённой стоимости)
- [x] 3.4 Тесты: `tests/unit/test_tui_app.py` (предупреждения; отчёт `/usage` до вопроса,
      после вопросов, после `/clear`, расход истории из файла; итоги сессии в статус-баре);
      `tests/unit/test_commands_screen.py` (команда в списке)

## 4. E2E

- [x] 4.1 `tests/e2e/stub_api.py`: поле `finish_reason` в ответах
- [x] 4.2 `tests/e2e/test_commands_flow.py`: сценарий `/usage` после вопроса (отчёт на экране,
      ноль запросов к stub); сценарий предупреждения об усечении (stub отдаёт пустой `content` с
      `finish_reason=length`); проверка статус-бара с итогами; при необходимости — обновление
      снапшотов `--snapshot-update`

## 5. Финал

- [x] 5.1 Полный `pytest`; README проекта: документация `/usage`, `TABLETOP_HISTORY_LIMIT` и
      метрик в `history.json`
- [x] 5.2 `/opsx:archive`
