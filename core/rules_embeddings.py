"""Локальные embeddings для RAG через llama-server."""

import math
from dataclasses import dataclass
from typing import List, Optional

import requests

from . import config
from .rules_index import RulesIndexError


@dataclass(frozen=True)
class LocalEmbeddings:
    model: str
    url: str

    def embed(self, text: str) -> List[float]:
        try:
            response = requests.post(
                self.url, json={"model": self.model, "input": text}, timeout=120,
            )
            response.raise_for_status()
            data = response.json()["data"]
            if len(data) != 1 or data[0]["index"] != 0:
                raise ValueError("Неверный index в ответе")
            vector = data[0]["embedding"]
            if not isinstance(vector, list) or not vector or any(
                type(value) not in (int, float) or not math.isfinite(value) for value in vector
            ):
                raise ValueError("Неверный embedding-вектор")
            norm = math.hypot(*vector)
            if not norm or not math.isfinite(norm):
                raise ValueError("Нулевая или некорректная длина вектора")
            return [value / norm for value in vector]
        except (requests.RequestException, ValueError, KeyError, TypeError, OverflowError) as exc:
            raise RulesIndexError("Локальная embedding-модель {}: {}".format(self.model, exc)) from exc

    def embed_query(self, query: str) -> List[float]:
        # Qwen3-Embedding рекомендует instruction только для поисковых запросов.
        return self.embed(
            "Instruct: Given a question about board game rules, retrieve relevant rulebook passages.\n"
            "Query: " + query
        )


def for_model(chat_model: str) -> Optional[LocalEmbeddings]:
    if chat_model not in config.LOCAL_MODELS:
        return None
    models = config.LOCAL_EMBEDDING_MODELS
    if len(models) != 1:
        raise RulesIndexError(
            "Для локального RAG нужен ровно один embedding-пресет в llama_server/models.ini "
            "с embedding = true; найдено {}.".format(len(models))
        )
    chat_url = config.api_url_for_model(chat_model)
    return LocalEmbeddings(models[0], chat_url.rsplit("/chat/completions", 1)[0] + "/embeddings")
