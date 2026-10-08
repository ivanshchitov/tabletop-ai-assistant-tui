"""Профили основных ответов Qwen 2B; имя серверного пресета задаёт режим."""

from dataclasses import dataclass

from . import config
from .answer_settings import AnswerSettings


@dataclass(frozen=True)
class LocalReport:
    model: str
    profile: str
    quantization: str
    temperature: float
    max_tokens: int
    context_window: int


# Исходные идентификаторы сохранены для обратимого переключения.
MODELS = {
    "Q4_K_M": ("unsloth/Qwen3.5-2B-GGUF:Q4_K_M", "tabletop-qwen-2b-q4-k-m-optimized"),
    "Q4_K_XL": ("unsloth/Qwen3.5-2B-GGUF:Q4_K_XL", "tabletop-qwen-2b-q4-k-xl-optimized"),
}


def is_optimized(model: str) -> bool:
    return any(model == pair[1] for pair in MODELS.values())


def switch_model(model: str, profile: str) -> str:
    if profile not in ("baseline", "optimized"):
        raise ValueError("Использование: /local baseline|optimized")
    for pair in MODELS.values():
        if model in pair:
            return pair[profile == "optimized"]
    raise ValueError("Профили /local доступны для Qwen3.5-2B Q4_K_M и Q4_K_XL. Выберите её через /models.")


def report(model: str, settings: AnswerSettings) -> LocalReport:
    for quantization, pair in MODELS.items():
        if model in pair:
            optimized = model == pair[1]
            return LocalReport(
                model, "optimized" if optimized else "baseline", quantization,
                0.3 if optimized else settings.temperature,
                1024 if optimized else config.max_tokens_for_words(settings.max_words),
                8192 if optimized else 32000,
            )
    raise ValueError("Профили /local доступны для Qwen3.5-2B Q4_K_M и Q4_K_XL. Выберите её через /models.")
