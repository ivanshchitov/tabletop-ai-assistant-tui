"""Клиент для обращения к Deepseek Chat Completions API."""

import time
from typing import Optional

import requests

import config


class DeepseekAPIError(Exception):
    """Ошибка при обращении к Deepseek API."""


class DeepseekAPIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def ask(self, system_message: str, user_message: str) -> str:
        payload = {
            "model": config.MODEL_NAME,
            "messages": [
                {"role": "system", "content": system_message},
                {"role": "user", "content": f"Вопрос пользователя: {user_message}"},
            ],
            "temperature": config.TEMPERATURE,
            "max_tokens": config.MAX_TOKENS,
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
                raise DeepseekAPIError(
                    "Превышено время ожидания ответа от Deepseek API."
                ) from last_timeout
            except requests.exceptions.ConnectionError as exc:
                raise DeepseekAPIError(
                    "Ошибка соединения с Deepseek API. Проверьте интернет-соединение."
                ) from exc

            if response.status_code == 401:
                raise DeepseekAPIError("Неверный API-ключ Deepseek.")
            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError as exc:
                raise DeepseekAPIError(f"Ошибка Deepseek API: {exc}") from exc

            try:
                data = response.json()
                return data["choices"][0]["message"]["content"].strip()
            except (ValueError, KeyError, IndexError) as exc:
                raise DeepseekAPIError("Некорректный ответ от Deepseek API.") from exc

        raise DeepseekAPIError("Превышено время ожидания ответа от Deepseek API.")
