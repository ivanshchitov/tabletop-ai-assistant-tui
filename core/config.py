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

API_URL = "https://opencode.ai/zen/v1/chat/completions"
MODEL_NAME = "deepseek-v4-flash"
TEMPERATURE = 0.7

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
HISTORY_FILE = BASE_DIR / "history.json"

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3


def get_api_key() -> Optional[str]:
    return os.getenv("OPENCODE_API_KEY")


def set_api_key_runtime(api_key: str) -> None:
    os.environ["OPENCODE_API_KEY"] = api_key
