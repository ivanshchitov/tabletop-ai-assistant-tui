"""Сравнение считает реальные запросы и сохраняет ошибки и отсутствующие показатели."""

import importlib.util
from pathlib import Path

import pytest


def benchmark():
    path = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_local_llm.py"
    spec = importlib.util.spec_from_file_location("local_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summary_counts_errors_and_uses_total_request_time():
    module = benchmark()
    records = [
        {"status": "ok", "elapsed_seconds": 2.0, "completion_tokens": 20, "prompt_tokens": 10},
        {"status": "length", "elapsed_seconds": 4.0, "completion_tokens": 40, "prompt_tokens": 10},
        {"status": "error", "elapsed_seconds": 1.0, "completion_tokens": 0, "prompt_tokens": 0},
    ]
    summary = module.summarize(records)
    assert summary["requests"] == 3
    assert summary["complete_answers"] == 1
    assert summary["median_seconds"] == 3.0
    assert summary["tokens_per_second"] == 10.0
    assert summary["errors"] == 1 and summary["truncated"] == 1


def test_missing_metrics_do_not_become_zero_speed():
    module = benchmark()
    summary = module.summarize([{"status": "error", "elapsed_seconds": 0,
                                "completion_tokens": 0, "prompt_tokens": 0}])
    assert summary["tokens_per_second"] is None
    assert summary["median_seconds"] is None


def test_memory_sampling_only_counts_owned_process_group():
    module = benchmark()
    sample = "11 100 1024\n12 100 2048\n13 200 999999\n"
    assert module.group_rss_kib(sample, 100) == 3072
    assert module.group_rss_kib(sample, 300) is None


def test_run_question_preserves_seed_parameters_and_full_answer(tmp_path, monkeypatch):
    import json
    import responses
    module = benchmark()
    monkeypatch.setenv("TABLETOP_LOCAL_API_URL", "http://127.0.0.1:17777/v1/chat/completions")
    with responses.RequestsMock() as stub:
        stub.add(responses.POST, "http://127.0.0.1:17777/v1/chat/completions", json={
            "choices": [{"message": {"content": "Ответ полностью."}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        })
        record = module.run_question(tmp_path, "tabletop-qwen-2b-q4-k-m-optimized",
                                     {"id": "test", "question": "Как ходит конь?", "criterion": "Критерий"}, 42)
        payload = json.loads(stub.calls[0].request.body)
    assert record["status"] == "length"
    assert record["answer"] == "Ответ полностью."
    assert record["completion_tokens"] == 7
    assert record["seed"] == payload["seed"] == 42
    assert payload["cache_prompt"] is False
    assert record["parameters"]["context_window"] == 8192
    assert len(record["http_requests"]) == 1
    assert all(Path(record["state_paths"][key]).is_relative_to(tmp_path) for key in record["state_paths"])


def test_run_question_records_api_error(tmp_path, monkeypatch):
    import responses
    module = benchmark()
    monkeypatch.setenv("TABLETOP_LOCAL_API_URL", "http://127.0.0.1:17777/v1/chat/completions")
    with responses.RequestsMock() as stub:
        stub.add(responses.POST, "http://127.0.0.1:17777/v1/chat/completions", status=400)
        record = module.run_question(tmp_path, "unsloth/Qwen3.5-2B-GGUF:Q4_K_M",
                                     {"id": "test", "question": "Как ходит конь?", "criterion": "Критерий"}, 42)
    assert record["status"] == "error" and record["error"]
    assert record["answer"] == ""


def test_server_cleanup_does_not_signal_an_existing_listener(tmp_path, monkeypatch):
    import os
    module = benchmark()
    signals = []
    monkeypatch.setattr(os, "killpg", lambda *args: signals.append(args))
    server = module.BenchmarkServer(tmp_path)
    server.stop()
    assert signals == []
