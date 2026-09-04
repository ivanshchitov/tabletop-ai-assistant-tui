"""Загрузка конфигурации и переменных окружения."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# core/config.py -> подняться на уровень выше, к корню репозитория, где лежат
# .env, assets/ и history.json.
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_PATH)

# Базовый адрес переопределяется через окружение: это позволяет e2e-тестам поднять
# локальный stub-сервер вместо обращения к реальному OpenCode Zen.
DEFAULT_API_URL = "https://opencode.ai/zen/v1/chat/completions"
API_URL = os.getenv("OPENCODE_API_URL", DEFAULT_API_URL)
# Список моделей, которые пользователь может выбрать через /models; первая — модель
# по умолчанию. Панель и клиент читают отсюда, имена моделей не зашиваются нигде больше.
AVAILABLE_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "kimi-k2.5",
    "glm-5.1",
    "mimo-v2.5-free",
    "kimi-k2.6",
    "kimi-k3",
]
DEFAULT_MODEL = AVAILABLE_MODELS[0]
# Цена входных/выходных токенов (доллары за 1 млн токенов) по каталогу OpenCode Zen. Для моделей
# с разной ценой в пиковые/непиковые часы (DeepSeek V4 Flash/Pro) берётся более низкое (off-peak)
# значение — время суток на клиенте не отслеживается, точность не бухгалтерская, а сравнительная
# (см. openspec/changes/add-model-usage-metadata/design.md).
MODEL_PRICING = {
    "deepseek-v4-flash": (0.22, 0.66),
    "deepseek-v4-pro": (0.66, 1.98),
    "kimi-k2.5": (0.60, 3.00),
    "glm-5.1": (1.40, 4.40),
    "mimo-v2.5-free": (0.0, 0.0),
    "kimi-k2.6": (0.95, 4.00),
    "kimi-k3": (3.00, 15.00),
}
TEMPERATURE = 0.7
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0

MIN_MAX_WORDS = 10
MAX_MAX_WORDS = 500
DEFAULT_MAX_WORDS = 200

# Пользователь ограничивает ответ в словах (инструкция в user-промпте — см.
# prompts.build_user_prompt), а не в токенах API. Но запрос к API всё равно принимает
# технический `max_tokens` — используем его как запас, чтобы генерация не обрывалась
# посреди слова/предложения раньше, чем модель сама уложится в лимит по инструкции.
# Не основной рычаг управления длиной, поэтому запас намеренно щедрый.
WORDS_TO_TOKENS_RATIO = 4
TOKENS_OVERHEAD = 50


def max_tokens_for_words(max_words: int) -> int:
    return max_words * WORDS_TO_TOKENS_RATIO + TOKENS_OVERHEAD

# Условие завершения ответа: если ответ — список/подборка (например,
# рекомендации настольных игр), модель должна ограничиться этим числом
# вариантов и сразу завершить ответ (см. prompts.build_user_prompt).
# Настраивается пользователем через экран /settings — см. answer_settings.AnswerSettings.
DEFAULT_LIST_LIMIT = 3
MIN_LIST_LIMIT = 1
MAX_LIST_LIMIT = 10

ASSETS_DIR = BASE_DIR / "assets"

MAX_INPUT_LENGTH = 2000
HISTORY_LIMIT = 50
# Путь к истории тоже переопределяется через окружение. Он выводится из __file__, а не
# из текущего каталога, поэтому без такого переключателя любой прогон приложения (в том
# числе тестовый) писал бы в единственный реальный history.json в корне репозитория.
DEFAULT_HISTORY_FILE = BASE_DIR / "history.json"
HISTORY_FILE = Path(os.getenv("TABLETOP_HISTORY_FILE", str(DEFAULT_HISTORY_FILE)))

# Таймаут переопределяется через окружение: e2e-сценарию с ретраями нужно, чтобы клиент
# сдавался за доли секунды, а не ждал полминуты на каждый намеренно зависший ответ.
REQUEST_TIMEOUT = int(os.getenv("TABLETOP_REQUEST_TIMEOUT", "30"))
MAX_RETRIES = 3


def get_api_key() -> Optional[str]:
    return os.getenv("OPENCODE_API_KEY")


def set_api_key_runtime(api_key: str) -> None:
    os.environ["OPENCODE_API_KEY"] = api_key
