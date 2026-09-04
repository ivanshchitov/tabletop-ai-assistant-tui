"""Клиент для обращения к Chat Completions API."""

import json
import re
import time
from typing import Optional

import requests

from . import config

_JSON_CODE_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def is_valid_json_answer(text: str) -> bool:
    """Проверяет, что ответ — валидный JSON (в т.ч. внутри блока ```json)."""
    match = _JSON_CODE_BLOCK_RE.search(text)
    candidate = match.group(1) if match else text
    try:
        json.loads(candidate)
        return True
    except (ValueError, TypeError):
        return False


def is_valid_api_key(api_key: str) -> bool:
    """Проверяет, что ключ пригоден для HTTP-заголовка.

    Заголовки кодируются latin-1, поэтому ключ с кириллицей (частая опечатка — набор в русской
    раскладке) обрывал бы запрос UnicodeEncodeError глубоко внутри requests, то есть падением с
    traceback вместо понятного сообщения. Проверяем заранее и сами.
    """
    return bool(api_key) and api_key.isascii()


API_KEY_CHARSET_ERROR = (
    "API-ключ содержит недопустимые символы. Допустимы только латинские буквы, цифры и знаки "
    "пунктуации — проверьте раскладку клавиатуры."
)


class APIError(Exception):
    """Ошибка при обращении к API."""


class APIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def ask(
        self,
        system_message: str,
        user_message: str,
        max_tokens: int = config.max_tokens_for_words(config.DEFAULT_MAX_WORDS),
        temperature: float = config.TEMPERATURE,
        model: str = config.DEFAULT_MODEL,
    ) -> str:
        if not is_valid_api_key(self.api_key):
            raise APIError(API_KEY_CHARSET_ERROR)

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_timeout: Optional[Exception] = None

        for attempt in range(config.MAX_RETRIES):
            try:
                response = requests.post(
                    config.API_URL, json=payload, headers=headers, timeout=config.REQUEST_TIMEOUT
                )
            except requests.exceptions.Timeout as exc:
                last_timeout = exc
                if attempt < config.MAX_RETRIES - 1:
                    time.sleep(2**attempt)
                    continue
                raise APIError(
                    "Превышено время ожидания ответа от API."
                ) from last_timeout
            except requests.exceptions.ConnectionError as exc:
                raise APIError(
                    "Ошибка соединения с API. Проверьте интернет-соединение."
                ) from exc

            if response.status_code == 401:
                raise APIError("Неверный API-ключ.")
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                raise APIError(f"Ошибка API: {exc}") from exc

            try:
                data = response.json()
                return data["choices"][0]["message"]["content"].strip()
            except (ValueError, KeyError, IndexError) as exc:
                raise APIError("Некорректный ответ от API.") from exc

        raise APIError("Превышено время ожидания ответа от API.")
