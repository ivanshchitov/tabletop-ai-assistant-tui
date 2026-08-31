"""Загрузка конфигурации и переменных окружения."""

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_PATH)

API_URL = "https://opencode.ai/zen/v1/chat/completions"
MODEL_NAME = "deepseek-v4-flash"
TEMPERATURE = 0.7
MAX_TOKENS = 1000

MAX_INPUT_LENGTH = 2000
HISTORY_LIMIT = 50
HISTORY_FILE = BASE_DIR / "history.json"

REQUEST_TIMEOUT = 30
MAX_RETRIES = 3


def get_api_key() -> Optional[str]:
    return os.getenv("OPENCODE_API_KEY")


def set_api_key_runtime(api_key: str) -> None:
    os.environ["OPENCODE_API_KEY"] = api_key
