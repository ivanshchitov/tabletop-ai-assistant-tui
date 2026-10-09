"""Настоящий HTTP-шлюз против локальной HTTP-заглушки модели."""

from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest
import requests


@contextmanager
def running(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.fixture
def service():
    from llm_service.backend import Backend
    from llm_service.http_server import ServiceServer
    from llm_service.policy import Policy

    state = {"tokens": 20, "calls": [], "blocked": False, "failure": None,
             "entered": threading.Event(), "release": threading.Event(), "now": 100.0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond({"status": "ok"})

        def respond(self, body, status=200):
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except OSError:
                pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["calls"].append((self.path, body))
            if self.path == "/apply-template":
                self.respond({"prompt": "formatted"})
            elif self.path == "/tokenize":
                self.respond({"tokens": [1] * state["tokens"]})
            else:
                if state["blocked"]:
                    state["entered"].set()
                    state["release"].wait(3)
                if state["failure"]:
                    self.respond({"private": "do not disclose"}, 500)
                else:
                    self.respond({"choices": [{"message": {"role": "assistant", "content": "Ответ CATAN"},
                                                "finish_reason": "stop"}],
                                  "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23}})

    with running(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as backend_url:
        policy = Policy("http-test-secret", clock=lambda: state["now"])
        state["backend"] = Backend(backend_url)
        server = ServiceServer(("127.0.0.1", 0), policy, state["backend"])
        state["server"] = server
        with running(server) as url:
            yield url, state, policy


def post(url, **fields):
    return requests.post(url + "/v1/chat/completions", timeout=5,
                         headers={"Authorization": "Bearer http-test-secret"},
                         json={"messages": [{"role": "user", "content": "CATAN?"}], **fields})


def test_http_authorization_health_and_chat(service, caplog):
    caplog.set_level("INFO", logger="tabletop.llm_service")
    url, state, _ = service
    assert requests.get(url + "/health", timeout=5).status_code == 200
    assert requests.get(url + "/v1/models", timeout=5).status_code == 401
    assert requests.post(url + "/v1/chat/completions", json={}, timeout=5).status_code == 401
    models = requests.get(url + "/v1/models", headers={"Authorization": "Bearer http-test-secret"}, timeout=5)
    assert models.status_code == 200
    assert len(models.json()["data"]) == 1
    response = post(url)
    assert response.status_code == 200
    assert response.json()["usage"]["total_tokens"] == 23
    history = [{"role": "user", "content": "CATAN?"}, response.json()["choices"][0]["message"],
               {"role": "user", "content": "А сколько очков нужно для победы?"}]
    assert post(url, messages=history).status_code == 200
    assert state["calls"][-1][1]["messages"] == history
    assert "http-test-secret" not in caplog.text
    assert "А сколько очков" not in caplog.text
    assert "HTTP 200" in caplog.text


def test_http_invalid_body_unknown_route_and_size_limit(service):
    url, state, _ = service
    assert post(url, max_tokens=1025).status_code == 400
    assert post(url, stream=True).status_code == 400
    headers = {"Authorization": "Bearer http-test-secret", "Content-Type": "application/json"}
    assert requests.post(url + "/v1/chat/completions", headers=headers, data="{", timeout=5).status_code == 400
    assert requests.post(url + "/v1/chat/completions", headers=headers, data="x" * 131073, timeout=5).status_code == 413
    assert requests.get(url + "/props", timeout=5).status_code == 404
    assert not state["calls"]
    assert post(url).status_code == 200


def test_tui_uses_private_service_for_two_chat_turns(app, service):
    from . import harness
    url, state, _ = service
    with app(api_key="", auto_tools=False, extra_env={
        "TABLETOP_LOCAL_API_URL": url + "/v1/chat/completions",
        "TABLETOP_LOCAL_API_KEY": "http-test-secret",
    }) as session:
        session.wait_for_prompt()
        session.send_line("/models")
        session.wait_on_screen("Enter — применить")
        session.send_key(harness.KEY_DOWN, 5)
        session.send_key(harness.KEY_ENTER)
        session.wait_until_gone("Enter — применить")
        session.wait_for("Модель: unsloth/Qwen3.5-2B-GGUF:Q4_K_M")
        session.send_line("/local optimized")
        session.wait_for("Профиль локальной LLM: optimized")
        session.send_line("Сколько стоит дорога в CATAN?")
        session.wait_for("Ответ CATAN")
        harness.wait_for_answers(session, 1, marker="Токены: 20+3=23")
        session.send_line("А сколько очков нужно для победы в этой игре?")
        harness.wait_for_answers(session, 2, marker="Токены: 20+3=23")
        completions = [body for path, body in state["calls"] if path == "/v1/chat/completions"]
        assert len(completions) == 2
        assert any(message["role"] == "assistant" and message["content"] == "Ответ CATAN"
                   for message in completions[-1]["messages"])
        session.send_line("/exit")
        session.wait_exit()
    assert requests.get(url + "/health", timeout=5).status_code == 200


def test_http_context_rate_limit_and_recovery(service):
    url, state, _ = service
    state["tokens"] = 8192
    for _ in range(10):
        response = post(url)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "context_length_exceeded"
    response = post(url)
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert not any(path == "/v1/chat/completions" for path, _ in state["calls"])
    state["now"] += 60
    state["tokens"] = 20
    assert post(url).status_code == 200


def test_http_queue_overload_and_backend_failure_recover(service):
    import time
    url, state, policy = service
    state["blocked"] = True
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(post, url)
            assert state["entered"].wait(2)
            second = pool.submit(post, url)
            deadline = time.monotonic() + 2
            while policy._places._value != 0 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert policy._places._value == 0
            response = post(url)
            assert response.status_code == 503
            assert response.headers["Retry-After"] == "1"
            state["release"].set()
            assert first.result().status_code == 200
            assert second.result().status_code == 200
    finally:
        state["release"].set()
    state["blocked"] = False
    state["failure"] = True
    response = post(url)
    assert response.status_code == 502
    assert "do not disclose" not in response.text
    state["failure"] = None
    assert post(url).status_code == 200


def test_slow_body_has_absolute_deadline_and_releases_handler(service, monkeypatch):
    import socket
    import time
    from urllib.parse import urlsplit
    from llm_service import http_server
    monkeypatch.setattr(http_server, "READ_TIMEOUT", 0.2, raising=False)
    url, _, _ = service
    address = urlsplit(url)
    body = b" " * 8 + b'{"messages":[{"role":"user","content":"CATAN?"}]}'
    with socket.create_connection((address.hostname, address.port), timeout=2) as sock:
        sock.sendall(("POST /v1/chat/completions HTTP/1.0\r\n"
                      "Authorization: Bearer http-test-secret\r\nContent-Type: application/json\r\n"
                      f"Content-Length: {len(body)}\r\n\r\n").encode())
        try:
            for _ in range(8):
                sock.sendall(b" ")
                time.sleep(0.05)
            sock.sendall(body[8:])
        except OSError:
            pass
        response = sock.recv(4096)
    assert b" 408 " in response.split(b"\r\n", 1)[0]
    assert post(url).status_code == 200


def test_backend_timeout_returns_504_and_releases_slot(service):
    url, state, _ = service
    state["backend"].timeout = 0.05
    state["blocked"] = True
    try:
        response = post(url)
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "backend_timeout"
    finally:
        state["release"].set()
        state["blocked"] = False
        state["backend"].timeout = 300
    assert post(url).status_code == 200


def test_eight_slow_clients_cannot_create_unbounded_handlers(service):
    import socket
    import time
    from urllib.parse import urlsplit
    url, state, _ = service
    address = urlsplit(url)
    sockets = []
    try:
        for _ in range(8):
            sock = socket.create_connection((address.hostname, address.port), timeout=2)
            sockets.append(sock)
            sock.sendall(b"POST /v1/chat/completions HTTP/1.0\r\n"
                         b"Authorization: Bearer http-test-secret\r\n"
                         b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n")
        deadline = time.monotonic() + 2
        while state["server"]._handlers._value != 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert state["server"]._handlers._value == 0
        response = requests.get(url + "/health", timeout=2)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
    finally:
        for sock in sockets:
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
