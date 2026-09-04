"""Расчёт стоимости запроса по таблице цен моделей."""

from typing import Optional

from . import config


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
    pricing = config.MODEL_PRICING.get(model)
    if pricing is None:
        return None
    input_price, output_price = pricing
    return (prompt_tokens * input_price + completion_tokens * output_price) / 1_000_000
