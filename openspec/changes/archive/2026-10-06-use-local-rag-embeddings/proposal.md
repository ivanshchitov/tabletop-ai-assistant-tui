## Why

В llama-server настроена Qwen3-Embedding, но RAG всегда использует хеш-векторы. При выборе локальной модели поиск должен использовать локальную embedding-модель для документов и запросов.

## What Changes

- Определять embedding-пресет по `embedding = true` в `llama_server/models.ini`; исключить его из `/models`.
- При локальной модели чата использовать `/v1/embeddings` llama-server без облачного ключа; для облачных моделей сохранить текущий хеш-поиск.
- Хранить векторы разных провайдеров раздельно. Для существующего индекса подготовить локальные векторы из сохранённых чанков при первом локальном поиске, без повторного чтения PDF; не выполнять запросы при запуске или при отключённом RAG.
- Ошибки embedding-сервера показывать как ошибку RAG без молчаливой подмены на хеш-поиск; сохранять предыдущий индекс.
- `/rules index` и `/rules compare` учитывать выбранную модель. Меняется контракт: при локальной модели эти команды могут обращаться к embedding API, но не к chat API.

## Capabilities

### New Capabilities

- `local-rag-embeddings`: выбор embedding-пресета, согласованность векторов, совместимость существующего индекса и обработка ошибок.

### Modified Capabilities

- `rules-document-index`: разрешить embedding-запросы команд индексации и сравнения при локальной модели.

## Impact

Файлы: `core/config.py`, новый `core/rules_embeddings.py`, `core/rules_index.py`, `core/tabletop_agent.py`, `core/rules_retrieval.py` при необходимости нового статуса ошибки, `ui/tui_app.py`, `tests/unit/test_config.py`, `tests/unit/test_rules_embeddings.py`, `tests/unit/test_rules_index.py`, `tests/unit/test_tabletop_agent.py`, `tests/unit/test_tui_app.py`, `README.md`, `CLAUDE.md`.

Критерии: локальные документы и запрос используют один embedding-пресет; облачный путь и старый индекс продолжают работать; смена модели не смешивает пространства векторов; embedding-пресет отсутствует в выборе чата; ошибки не портят индекс. Новые зависимости не требуются. Реализацию и тесты можно объединить в один коммит после подтверждения.
