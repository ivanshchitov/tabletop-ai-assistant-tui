"""Выбор квантования и профиля в настоящем терминале; запрос идёт в локальный API."""

import pytest

from . import harness


@pytest.mark.parametrize("steps,quant,optimized", [
    (5, "Q4_K_M", "tabletop-qwen-2b-q4-k-m-optimized"),
    (7, "Q4_K_XL", "tabletop-qwen-2b-q4-k-xl-optimized"),
])
def test_switch_profile_keeps_quantization_and_routes_request(app, stub, steps, quant, optimized):
    session = app(extra_env={"TABLETOP_LOCAL_API_URL": stub.url})
    session.wait_for_prompt()
    session.send_line("/models")
    session.wait_on_screen("Enter — применить")
    session.send_key(harness.KEY_DOWN, steps)
    session.send_key(harness.KEY_ENTER)
    session.wait_until_gone("Enter — применить")
    session.wait_for("Модель: unsloth/Qwen3.5-2B-GGUF:" + quant)
    session.send_line("/local optimized")
    session.wait_for("Профиль локальной LLM: optimized")
    session.wait_for("Контекст сервера: 8192 ток.")
    assert stub.call_count == 0
    session.send_line("Как ходит конь в шахматах?")
    session.wait_for("Ответ stub-сервера.")
    payload = stub.last_payload()
    assert payload["model"] == optimized
    assert (payload["temperature"], payload["max_tokens"]) == (0.3, 1024)
    assert "Authorization" not in stub.requests[0]["headers"]
    session.send_line("/local baseline")
    session.wait_for("Профиль локальной LLM: baseline")
    session.send_line("/exit")
    session.wait_exit()
