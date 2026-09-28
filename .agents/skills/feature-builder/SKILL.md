---
name: feature-builder
description: Использовать, когда пользователь просит создать, добавить или реализовать фичу в этом проекте («создай фичу», «добавь команду /x», «реализуй новую функцию», «new feature», «implement feature») — любое изменение наблюдаемого поведения, которое по CLAUDE.md идёт через OpenSpec. Не использовать для опечаток и однострочных фиксов и для заданий челленджа с видео (тогда solve-challenge-task, который вызывает этот скилл сам).
---

# Разработка фичи: OpenSpec → ветка → TDD → архив → ff-merge

Стек: Python 3.9+, `pytest`. Один проход = одно OpenSpec-изменение `<change-id>` (kebab-case, глагол первым: `add-export-command`).

Стиль общения — caveman: строки статуса, без пересказа кода. Пример: `Тест упал: ImportError core.exporter. Реализовал. pytest: OK.`

Грабли цикла (`--yes` у archive, валидация спек, новый файл состояния, объём прогонов, конвенции коммитов) — блок в `CLAUDE.md` проекта; читать его до старта.
Команды `/opsx:*` в `CLAUDE.md` относятся к Claude Code; в Codex использовать команды `openspec` ниже.

## Этап 1. Спека (кода нет)

1. Если из запроса неясно ожидаемое поведение или затронутый код — сначала исследовать код и спеки и дать короткое резюме (требования, ограничения, допущения); в остальных случаях этап пропускается.
2. Вывести `<change-id>` из описания и вызвать `openspec new change "<change-id>"`. Затем `openspec status --change "<change-id>" --json` показывает порядок и пути артефактов. Для каждого готового артефакта вызвать `openspec instructions <artifact-id> --change "<change-id>" --json`, создать файл по выданному пути и повторить `status`, пока готовы все артефакты, от которых зависит реализация. Проверить зависимости в `status`: один готовый `tasks.md` сам по себе не доказывает полноту плана. В стандартной схеме артефакты лежат в `openspec/changes/<change-id>/` (`proposal.md`, `design.md`, `specs/<capability>/spec.md`, `tasks.md`).
3. В `proposal.md` — суть, изменяемые файлы (включая `tests/**/test_*.py`), критерии приёмки. `tasks.md` — и есть план работ; отдельный план (`writing-plans`, `docs/plans/`) не создаётся.
4. `openspec validate "<change-id>" --strict` зелёный. Частые отказы:
   - `MODIFIED`-блок обязан **повторить все** сценарии требования из основной спеки — archive откажется их терять;
   - `## Purpose` пишется только в новой capability; у существующей Purpose правится прямо в `openspec/specs/<capability>/spec.md`.
5. **Стоп 1**: путь к `proposal.md` + просьба подтвердить. До подтверждения код не пишется.

Если `openspec/changes/` уже содержит незаархивированное изменение — сказать об этом на Стопе 1; в `openspec archive` всегда передавать `<change-id>` явно.

## Этап 2. Ветка

`git switch main && git pull --ff-only && git switch -c <change-id>`. Именно ветка, не worktree: e2e-тесты запускают приложение из корня репозитория (`.env`, `history.json`, `REPO_ROOT` в `tests/e2e/harness.py`).

## Этап 3. Реализация

Вызвать `openspec instructions apply --change "<change-id>" --json` и проверить `state`, даже если команда завершилась с кодом 0: `blocked` означает недостающие артефакты, `ready` — можно выполнять задачи, `all_done` — переходить к закрытию.

Для каждой задачи из `tasks.md`:

1. Падающий тест (`tests/unit/` или `tests/e2e/`), `pytest tests/unit -q` — красный.
2. Минимальный код, `pytest tests/unit -q` — зелёный; e2e — `pytest tests/e2e/test_<x>.py -q`, снапшоты обновлять только при осознанной смене раскладки (`--snapshot-update`).
3. Отметить задачу `[x]` в `tasks.md`.
4. Тест красный после правки — прочитать вывод целиком, одна гипотеза, одна правка. Красный после двух правок — **superpowers:systematic-debugging** (воспроизвести, найти причину, потом чинить), не третья правка вслепую.

**Объём прогонов.** В работе — файл затронутого слоя (`pytest tests/unit -q` ≈ 3 с, `pytest tests/e2e/test_<x>.py -q` ≈ 10 с). Полный `pytest -q` (unit + e2e, ≈ 100 с) — один раз, в Этапе 4. Гонять всю батарею после каждой правки — потерянные минуты, а не осторожность.

**REQUIRED BACKGROUND:** superpowers:test-driven-development — тест до кода, без исключений «слишком просто» и «допишу потом».

### Что тестировать отдельно

- **Существующий тест расходится с новым поведением** ⇒ это смена контракта: обновить осознанно, оставить в тесте комментарий «почему изменилось» и назвать смену на Стопе 1. Подгонка формулировок под новый текст — не то же самое, что осознанная смена семантики; подгонять молча нельзя.

**Коммит — только после «да».** Перед каждым: `git status`, `git diff --stat`, `git add` по путям фичи (не `-A`: в дереве могут лежать посторонние правки), два варианта сообщения — первый в стиле последних `git log --format=%s -10`, второй Conventional Commits (если в проекте уже выбран свой стиль, он идёт первым), пользователь может ввести свой. Логически связанные задачи допустимо коммитить одним коммитом — предложить группировку на Стопе 1.

## Этап 4. Закрытие

1. Полный `pytest -q` (unit + e2e). Зелёный = свежий вывод в этой сессии (superpowers:verification-before-completion), не память о прошлом прогоне. Перед архивом повторить `openspec validate "<change-id>" --strict`.
2. `openspec archive "<change-id>" --yes` (дельта уходит в `openspec/specs/`, флаг обязателен: без TTY CLI падает на подтверждении), коммит архива — по подтверждению. Если архив создал новую capability с шаблонным `## Purpose`, заполнить его в основной спеке. Архив меняет только спецификации, поэтому полный `pytest` после него не нужен — достаточно `openspec validate --all --strict`.
3. `CLAUDE.md` и `README.md`: не только добавить новое, но и **сверить существующие списки с реальностью** — команды, перечень capabilities, модули, переменные окружения, структура проекта, test layout. Устаревшая строка в документации — такой же дефект, как ошибка в коде (в дне 11 нашлась capability `prompt-strategies`, удалённая днём раньше). Отдельный коммит — по подтверждению.
4. Слияние — по подтверждению, показав `git log main..<change-id> --oneline`:

   ```bash
   git rebase main                 # на ветке, если main ушёл вперёд
   git switch main && git merge --ff-only <change-id> && git branch -d <change-id>
   ```

   Только fast-forward; merge-коммиты и `--no-ff` не используются.

## Superpowers: что в цикле, что нет

В цикле: `superpowers:test-driven-development` (Этап 3), `superpowers:systematic-debugging` (после двух красных правок), `superpowers:verification-before-completion` (Этап 4).

Вне цикла — их роль уже закрывает OpenSpec или правило скилла:
`superpowers:brainstorming` → исследование из Этапа 1 при неясных требованиях; `superpowers:writing-plans`, `superpowers:executing-plans`, `superpowers:subagent-driven-development` → `tasks.md` + Этап 3; `superpowers:using-git-worktrees` → ветка `git switch -c`; `superpowers:finishing-a-development-branch` → ff-слияние из Этапа 4. Не вызывать их только из-за формулировки «MUST» в их описании.

## Частые ошибки

| Ошибка | Правильно |
|---|---|
| Создать `openspec/proposals/` или `openspec/tasks.md` | Всё внутри `openspec/changes/<change-id>/` |
| Изоляция через `superpowers:using-git-worktrees` | Обычная ветка `git switch -c` |
| Дублировать `tasks.md` планом `writing-plans` | `tasks.md` — единственный план |
| `openspec archive` без имени при двух активных изменениях | Всегда `openspec archive "<change-id>" --yes` |
| `openspec archive` без `--yes` в неинтерактивной среде | `openspec archive "<change-id>" --yes` |
| `MODIFIED`-блок с частью сценариев требования | Повторить требование целиком, проверить `validate --strict` |
| Полный `pytest` после архивации спек | `openspec validate --all --strict` — архив код не меняет |
| Полная батарея после каждой правки | Файл затронутого слоя; полный прогон один раз, в Этапе 4 |
| Правило-эвристика с одним положительным тестом | Положительный и отрицательный тест на каждое правило |
| Коммит/мерж «раз тесты зелёные» | Показать status + diff --stat, ждать «да» |
| Вызвать `superpowers:brainstorming` «потому что MUST» | Исследовать код и спеки, только если требования неясны |
| Начать реализацию при `state: blocked` | Вернуться к недостающим артефактам из `openspec status --change "<change-id>" --json` |
