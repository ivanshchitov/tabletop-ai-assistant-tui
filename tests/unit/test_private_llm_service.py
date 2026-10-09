"""Контракт сервиса: границы, приватность и освобождение очереди."""

import pytest


def test_service_requires_a_header_safe_key():
    from llm_service.policy import Policy
    for key in ("", " ", "секрет", "abc\nvalue"):
        with pytest.raises(ValueError):
            Policy(key)


def test_authorization_rejects_missing_wrong_and_unicode_keys():
    from llm_service.policy import Policy, ServiceError
    policy = Policy("test-secret")
    for header in (None, "Bearer wrong", "Bearer секрет", "Basic test-secret"):
        with pytest.raises(ServiceError) as error:
            policy.authorize(header)
        assert error.value.status == 401
    policy.authorize("Bearer test-secret")


@pytest.mark.parametrize("override", [
    {"max_tokens": 1025}, {"max_tokens": 0}, {"max_tokens": True},
    {"temperature": float("nan")}, {"temperature": True}, {"temperature": 2.1}, {"temperature": 10**400},
    {"messages": []}, {"messages": [{"role": "tool", "content": "x"}]},
    {"messages": [{"role": "user", "content": ["x"]}]},
    {"stream": True}, {"model": "another"}, {"tools": []}, {"seed": False},
])
def test_invalid_generation_parameters_are_rejected(override):
    from llm_service.policy import Policy, ServiceError
    with pytest.raises(ServiceError) as error:
        Policy("test").validate({"messages": [{"role": "user", "content": "x"}], **override})
    assert error.value.status == 400


def test_system_messages_merge_without_mutating_client_history():
    from llm_service.policy import Policy
    messages = [{"role": "system", "content": "first"},
                {"role": "user", "content": "question"},
                {"role": "system", "content": "second"}]
    payload = Policy("test").validate({"messages": messages, "max_tokens": 1024})
    assert payload["messages"] == [{"role": "system", "content": "first\n\nsecond"},
                                   {"role": "user", "content": "question"}]
    assert len(messages) == 3
    assert payload["max_tokens"] == 1024


def test_assistant_reasoning_metadata_can_be_reused_in_chat():
    from llm_service.policy import Policy
    history = [{"role": "user", "content": "Название игры?"},
               {"role": "assistant", "content": "Лунный порт", "reasoning_content": "internal thinking"},
               {"role": "user", "content": "Повтори название"}]
    payload = Policy("test").validate({"messages": history})
    assert payload["messages"][1] == {"role": "assistant", "content": "Лунный порт"}
    assert history[1]["reasoning_content"] == "internal thinking"


def test_rate_limit_uses_sliding_window_and_retry_after():
    from llm_service.policy import Policy, ServiceError
    now = [100.0]
    policy = Policy("test", clock=lambda: now[0])
    for _ in range(10):
        policy.admit()
    with pytest.raises(ServiceError) as error:
        policy.admit()
    assert error.value.status == 429
    assert error.value.retry_after == 60
    now[0] = 159.1
    with pytest.raises(ServiceError) as error:
        policy.admit()
    assert error.value.retry_after == 1
    now[0] = 160.0
    policy.admit()


def test_context_reserves_answer_budget_at_boundary():
    from llm_service.policy import Policy, ServiceError
    policy = Policy("test")
    policy.check_context(7166, 1024)
    with pytest.raises(ServiceError) as error:
        policy.check_context(7167, 1024)
    assert error.value.code == "context_length_exceeded"


def test_service_queue_rejects_third_request_and_recovers_after_error():
    import threading
    import time
    from llm_service.policy import Policy, ServiceError
    policy = Policy("test")
    finished = threading.Event()

    def second():
        with policy.slot():
            finished.set()

    with policy.slot():
        thread = threading.Thread(target=second)
        thread.start()
        # Синхронизация по занятому месту, чтобы третий запрос не обогнал второй.
        deadline = time.monotonic() + 2
        while policy._places._value != 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert policy._places._value == 0
        with pytest.raises(ServiceError) as error:
            with policy.slot():
                pytest.fail("third request must not execute")
        assert error.value.status == 503
        assert not finished.is_set()
    thread.join(2)
    assert finished.is_set()
    with pytest.raises(RuntimeError):
        with policy.slot():
            raise RuntimeError("backend failed")
    with policy.slot():
        pass


def test_backend_preflight_prevents_generation_of_oversized_context():
    import responses
    from llm_service.backend import Backend
    from llm_service.policy import Policy, ServiceError
    with responses.RequestsMock() as http:
        http.post("http://backend/apply-template", json={"prompt": "formatted"})
        http.post("http://backend/tokenize", json={"tokens": [1] * 8192})
        with pytest.raises(ServiceError) as error:
            Backend("http://backend").complete(
                Policy("test").validate({"messages": [{"role": "user", "content": "x"}]}),
                Policy("test"),
            )
        assert error.value.code == "context_length_exceeded"
        assert len(http.calls) == 2


@pytest.mark.parametrize("failure,status", [("timeout", 504), ("connection", 502), ("malformed", 502)])
def test_backend_errors_do_not_disclose_raw_details(failure, status):
    import requests
    import responses
    from llm_service.backend import Backend
    from llm_service.policy import Policy, ServiceError
    with responses.RequestsMock() as http:
        response = {"timeout": requests.Timeout("private-key"),
                    "connection": requests.ConnectionError("private-key"),
                    "malformed": "private-key"}[failure]
        http.post("http://backend/apply-template", body=response)
        with pytest.raises(ServiceError) as error:
            Backend("http://backend").complete(
                Policy("test").validate({"messages": [{"role": "user", "content": "x"}]}),
                Policy("test"),
            )
        assert error.value.status == status
        assert "private-key" not in str(error.value)


def test_model_process_refuses_an_existing_listener():
    import socket
    from llm_service.runtime import ModelProcess
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        process = ModelProcess(port=listener.getsockname()[1])
        with pytest.raises(RuntimeError, match="занят"):
            process.start()
        assert process.process is None


def test_model_process_cleans_up_child_on_startup_timeout(tmp_path):
    import socket
    import sys
    from llm_service.runtime import ModelProcess
    sleeper = tmp_path / "slow-model"
    sleeper.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    sleeper.chmod(0o700)
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    process = ModelProcess(port=port, executable=str(sleeper), start_timeout=0.1)
    with pytest.raises(RuntimeError, match="готовности"):
        process.start()
    assert process.process.poll() is not None


def test_entry_point_rejects_missing_key_before_starting_model(monkeypatch, capsys):
    from llm_service.runtime import main
    monkeypatch.delenv("TABLETOP_SERVICE_API_KEY", raising=False)
    assert main(["--llama-command", "missing-command"]) == 1
    assert "TABLETOP_SERVICE_API_KEY" in capsys.readouterr().err


def test_model_process_stops_only_its_own_child(tmp_path, monkeypatch):
    import socket
    import sys
    from llm_service.runtime import ModelProcess
    fake = tmp_path / "fake-model"
    fake.write_text(f"#!{sys.executable}\n" + '''
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')
port = int(sys.argv[sys.argv.index('--port') + 1])
HTTPServer(('127.0.0.1', port), Handler).serve_forever()
''')
    fake.chmod(0o700)
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    process = ModelProcess(port=port, executable=str(fake), start_timeout=3)
    try:
        process.start()
        assert process.process.poll() is None
    finally:
        process.stop()
    assert process.process.poll() is not None
    monkeypatch.setattr("llm_service.runtime.os.killpg", lambda *args: pytest.fail("Повторный сигнал завершённому процессу"))
    process.stop()
