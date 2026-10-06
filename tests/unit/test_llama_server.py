"""Жизненный цикл на настоящем локальном HTTP-процессе без загрузки GGUF."""

import importlib.util
import io
from pathlib import Path
import socket
import sys

import pytest
import requests

from core import config


@pytest.fixture
def server_script(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    worker = tmp_path / "worker.py"
    worker.write_text(
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.end_headers(); self.wfile.write(b'{\"status\":\"ok\"}')\n"
        " def log_message(self, *args): pass\n"
        f"HTTPServer(('127.0.0.1', {port}), Handler).serve_forever()\n",
        encoding="utf-8",
    )
    script = tmp_path / "server.sh"
    import shlex
    script.write_text(f"#!/bin/bash\nexec {shlex.quote(sys.executable)} {shlex.quote(str(worker))}\n", encoding="utf-8")
    return script, f"http://127.0.0.1:{port}"


def manager(server_script, tmp_path):
    from core.llama_server import LlamaServer
    script, url = server_script
    return LlamaServer(script=script, base_url=url, log_path=tmp_path / "server.log", timeout=3)


def test_start_waits_for_health_and_stop_releases_listener(server_script, tmp_path):
    server = manager(server_script, tmp_path)
    try:
        server.start()
        assert requests.get(server.base_url + "/health", timeout=1).status_code == 200
        process = server.process
    finally:
        server.stop()
    assert process.poll() is not None
    with pytest.raises(requests.ConnectionError):
        requests.get(server.base_url + "/health", timeout=1)
    server.stop()  # повторное завершение безопасно


def test_early_process_exit_reports_log_and_cleans_up(server_script, tmp_path):
    script, _ = server_script
    script.write_text("echo 'bad model preset'\nexit 7\n")
    server = manager(server_script, tmp_path)
    with pytest.raises(RuntimeError, match="bad model preset"):
        server.start()
    assert server.process.poll() == 7


def test_start_timeout_terminates_spawned_process(server_script, tmp_path):
    script, _ = server_script
    script.write_text("exec sleep 30\n")
    server = manager(server_script, tmp_path)
    server.timeout = 0.1
    with pytest.raises(RuntimeError, match="готовности"):
        server.start()
    assert server.process.poll() is not None


def test_keyboard_interrupt_during_start_also_stops_process(server_script, tmp_path, monkeypatch):
    server = manager(server_script, tmp_path)
    monkeypatch.setattr(server, "_ready", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        server.start()
    assert server.process.poll() is not None


def test_existing_server_is_reused_and_stopped(server_script, tmp_path, monkeypatch):
    first = manager(server_script, tmp_path)
    second = manager(server_script, tmp_path)
    try:
        first.start()
        monkeypatch.setattr(second, "_listener_pid", lambda: first.process.pid)
        second.start()
        assert second.process is None  # нового экземпляра нет
        second.stop()
        first.process.wait(timeout=3)
    finally:
        first.stop()
    assert first.process.poll() is not None


def test_unrelated_listener_is_not_stopped(server_script, tmp_path):
    first = manager(server_script, tmp_path)
    second = manager(server_script, tmp_path)
    try:
        first.start()
        with pytest.raises(RuntimeError, match="не llama-server"):
            second.start()
        second.stop()
        assert first.process.poll() is None
    finally:
        first.stop()


def test_disabled_autostart_does_not_spawn(server_script, tmp_path, monkeypatch):
    server = manager(server_script, tmp_path)
    monkeypatch.setenv("TABLETOP_LLAMA_AUTOSTART", "0")
    server.start()
    server.stop()
    assert server.process is None


@pytest.mark.parametrize("error", [None, KeyboardInterrupt, RuntimeError])
def test_entrypoint_stops_server_after_tui_exit_without_cloud_key(server_script, tmp_path, monkeypatch, error):
    spec = importlib.util.spec_from_file_location("tabletop_entry", config.BASE_DIR / "tabletop-ai-assistant.py")
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    server = manager(server_script, tmp_path)
    monkeypatch.setattr(entry, "LlamaServer", lambda: server)
    monkeypatch.setattr(config, "get_api_key", lambda: None)

    class App:
        def __init__(self, client):
            assert client.api_key == ""
            from rich.console import Console
            self.console = Console(file=io.StringIO())
        def run(self):
            assert requests.get(server.base_url + "/health", timeout=1).status_code == 200
            if error:
                raise error()

    monkeypatch.setattr(entry, "TabletopAITUI", App)
    if error:
        with pytest.raises(error):
            entry.main()
    else:
        entry.main()
    assert server.process.poll() is not None
