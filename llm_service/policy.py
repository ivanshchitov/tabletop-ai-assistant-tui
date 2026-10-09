"""Авторизация и ограничение ресурсов одного приватного сервиса."""

from collections import deque
from contextlib import contextmanager
import hmac
import math
import threading
import time

MODEL = "tabletop-qwen-2b-q4-k-m-optimized"
MAX_BODY = 128 * 1024
CONTEXT_SIZE = 8192
MAX_OUTPUT = 1024


class ServiceError(Exception):
    def __init__(self, status, code, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after

    def body(self):
        return {"error": {"message": str(self), "type": "service_error", "code": self.code}}


class Policy:
    def __init__(self, key, *, clock=time.monotonic):
        if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ValueError("Задайте непустой ASCII-ключ TABLETOP_SERVICE_API_KEY без пробелов.")
        self._key = key.encode("ascii")
        self._clock = clock
        self._requests = deque()
        self._rate_lock = threading.Lock()
        self._places = threading.BoundedSemaphore(2)
        self._execution = threading.Lock()

    def authorize(self, header):
        supplied = (header or "").encode("utf-8")
        if not hmac.compare_digest(supplied, b"Bearer " + self._key):
            raise ServiceError(401, "unauthorized", "Неверный ключ приватного сервиса.")

    def validate(self, data):
        def invalid():
            raise ServiceError(400, "invalid_request", "Некорректные параметры текстового чата.")

        allowed = {"model", "messages", "max_tokens", "temperature", "seed", "cache_prompt", "stream"}
        if not isinstance(data, dict) or set(data) - allowed:
            invalid()
        if data.get("model", MODEL) != MODEL or data.get("stream", False) is not False:
            invalid()
        max_tokens = data.get("max_tokens", MAX_OUTPUT)
        temperature = data.get("temperature", 0.3)
        if type(max_tokens) is not int or not 1 <= max_tokens <= MAX_OUTPUT:
            invalid()
        if type(temperature) not in (int, float) or not 0 <= temperature <= 2 or not math.isfinite(temperature):
            invalid()
        if "seed" in data and (type(data["seed"]) is not int or not -1 <= data["seed"] <= 2**32 - 1):
            invalid()
        if "cache_prompt" in data and type(data["cache_prompt"]) is not bool:
            invalid()
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            invalid()
        systems, conversation = [], []
        for item in messages:
            if (not isinstance(item, dict) or not {"role", "content"} <= set(item)
                    or set(item) - {"role", "content", "reasoning_content"}
                    or item["role"] not in ("system", "user", "assistant")
                    or not isinstance(item["content"], str)):
                invalid()
            if "reasoning_content" in item and (item["role"] != "assistant"
                    or item["reasoning_content"] is not None and not isinstance(item["reasoning_content"], str)):
                invalid()
            if item["role"] == "system":
                systems.append(item["content"])
            else:
                conversation.append({"role": item["role"], "content": item["content"]})
        if not conversation or conversation[-1]["role"] != "user":
            invalid()
        normalized = ([{"role": "system", "content": "\n\n".join(systems)}] if systems else []) + conversation
        result = {"model": MODEL, "messages": normalized, "max_tokens": max_tokens,
                  "temperature": temperature, "stream": False}
        for option in ("seed", "cache_prompt"):
            if option in data:
                result[option] = data[option]
        return result

    def admit(self):
        with self._rate_lock:
            now = self._clock()
            while self._requests and now - self._requests[0] >= 60:
                self._requests.popleft()
            if len(self._requests) >= 10:
                retry = max(1, math.ceil(60 - (now - self._requests[0])))
                raise ServiceError(429, "rate_limit_exceeded", "Лимит: 10 запросов за 60 секунд.", retry)
            self._requests.append(now)

    def check_context(self, prompt_tokens, max_tokens):
        # Дополнительный запас BOS/EOS; backend также запрещает сдвиг контекста.
        if prompt_tokens + max_tokens + 2 > CONTEXT_SIZE:
            raise ServiceError(400, "context_length_exceeded", "Контекст и бюджет ответа превышают 8192 токена.")

    @contextmanager
    def slot(self):
        if not self._places.acquire(blocking=False):
            raise ServiceError(503, "queue_full", "Очередь сервиса заполнена.", 1)
        try:
            with self._execution:
                yield
        finally:
            self._places.release()
