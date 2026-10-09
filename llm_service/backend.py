"""Закрытый HTTP-клиент llama.cpp: шаблон, токены, генерация."""

import requests

from .policy import ServiceError


class Backend:
    def __init__(self, url="http://127.0.0.1:10099", timeout=300):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def ready(self):
        try:
            response = requests.get(self.url + "/health", timeout=2)
            return response.status_code == 200 and response.json().get("status") == "ok"
        except (requests.RequestException, ValueError, AttributeError):
            return False

    def _post(self, route, payload):
        try:
            response = requests.post(self.url + route, json=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("invalid response")
            return data
        except requests.Timeout as exc:
            raise ServiceError(504, "backend_timeout", "Модель не ответила вовремя.") from exc
        except (requests.RequestException, ValueError) as exc:
            raise ServiceError(502, "backend_unavailable", "Некорректный ответ или недоступность модели.") from exc

    def complete(self, payload, policy):
        template = self._post("/apply-template", {"messages": payload["messages"]})
        prompt = template.get("prompt")
        if not isinstance(prompt, str):
            raise ServiceError(502, "backend_unavailable", "Модель не вернула шаблон чата.")
        tokenized = self._post("/tokenize", {"content": prompt, "add_special": True, "parse_special": True})
        tokens = tokenized.get("tokens")
        if not isinstance(tokens, list) or any(type(token) is not int for token in tokens):
            raise ServiceError(502, "backend_unavailable", "Модель не вернула токены контекста.")
        policy.check_context(len(tokens), payload["max_tokens"])
        answer = self._post("/v1/chat/completions", payload)
        try:
            content = answer["choices"][0]["message"]["content"]
            counts = answer["usage"]
            if not isinstance(content, str) or any(type(counts[k]) is not int or counts[k] < 0
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")):
                raise ValueError("invalid completion")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ServiceError(502, "backend_unavailable", "Модель не вернула текстовый ответ и метрики.") from exc
        return answer
