"""Расчёт стоимости запроса по таблице цен моделей."""

from core import usage


def test_estimate_cost_known_model():
    cost = usage.estimate_cost("deepseek-v4-flash", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost == 0.22 + 0.66


def test_estimate_cost_unknown_model_returns_none():
    cost = usage.estimate_cost("no-such-model", prompt_tokens=100, completion_tokens=100)
    assert cost is None
