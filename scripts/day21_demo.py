#!/usr/bin/env python3
"""Локальное воспроизводимое демо индекса правил из отдельного окна терминала."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import signal
import struct
import sys
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMPT = "Введите вопрос (или /exit для выхода): ".encode("utf-8")
STEPS = [
    "/rules",
    "/rules index",
    "/rules compare Сколько стоит дорога в CATAN (road building costs 1 brick and 1 lumber)",
    "Сколько стоит дорога в CATAN (road building costs 1 brick and 1 lumber)?",
    "/exit",
]
ANSWER = (
    "В CATAN дорога строится за 1 древесину и 1 глину. "
    "Заплатите эти ресурсы, чтобы разместить дорогу на свободном участке пути."
)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = json.dumps(
            {
                "choices": [{"message": {"content": ANSWER}}],
                "usage": {"prompt_tokens": 620, "completion_tokens": 32, "total_tokens": 652},
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


def main() -> int:
    scratch = ROOT / "videos" / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    for name in (
        "day21-history.json",
        "day21-memory.json",
        "day21-profile.json",
        "day21-rules-index.sqlite3",
        "day21-rules-index.sqlite3-wal",
        "day21-rules-index.sqlite3-shm",
    ):
        (scratch / name).unlink(missing_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    os.write(
        sys.stdout.fileno(),
        "Демо day21: индекс и поиск работают с настоящими PDF; ответ даёт локальная заглушка API.\r\n".encode("utf-8"),
    )

    env = os.environ.copy()
    env.update(
        {
            "OPENCODE_API_KEY": "sk-demo-local-only",
            "OPENCODE_API_URL": f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
            "TABLETOP_MCP_COMMAND": str(ROOT / ".venv" / "bin" / "python"),
            "TABLETOP_MCP_ARGS": str(ROOT / "tests" / "fake_mcp_server.py"),
            "TABLETOP_HISTORY_FILE": str(scratch / "day21-history.json"),
            "TABLETOP_MEMORY_FILE": str(scratch / "day21-memory.json"),
            "TABLETOP_PROFILE_FILE": str(scratch / "day21-profile.json"),
            "TABLETOP_RULES_DOCUMENTS_DIR": str(ROOT / "docs" / "rules"),
            "TABLETOP_RULES_INDEX_FILE": str(scratch / "day21-rules-index.sqlite3"),
            "TABLETOP_TYPING_DELAY": "0.003",
            "TABLETOP_AUTO_TOOLS": "0",
        }
    )

    pid, master = pty.fork()
    if pid == 0:
        os.chdir(ROOT)
        try:
            terminal = termios.tcgetattr(0)
            terminal[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(0, termios.TCSANOW, terminal)
        except termios.error:
            pass
        os.execve(str(ROOT / ".venv" / "bin" / "python"), [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "tabletop-ai-assistant.py")], env)

    try:
        try:
            terminal = termios.tcgetattr(master)
            terminal[3] &= ~(termios.ECHO | termios.ECHONL)
            termios.tcsetattr(master, termios.TCSANOW, terminal)
        except termios.error:
            pass
        try:
            size = os.get_terminal_size(sys.stdout.fileno())
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", size.lines, size.columns, 0, 0))
        except OSError:
            pass

        seen = bytearray()
        step = 0
        deadline = time.monotonic() + 240
        while step < len(STEPS) and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 1)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                os.write(sys.stdout.fileno(), chunk)
                seen.extend(chunk)
            if PROMPT in seen:
                time.sleep(2.5)
                command = STEPS[step]
                line = command.encode("utf-8") + b"\n"
                for byte in line:
                    os.write(master, bytes((byte,)))
                    time.sleep(0.008)
                seen.clear()
                step += 1

        if step < len(STEPS):
            os.kill(pid, signal.SIGINT)
            return 2
        # Drain the final goodbye and let the child flush its terminal before returning.
        status = None
        finish_deadline = time.monotonic() + 8
        while time.monotonic() < finish_deadline:
            ready, _, _ = select.select([master], [], [], 0.25)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    os.write(sys.stdout.fileno(), chunk)
            waited, child_status = os.waitpid(pid, os.WNOHANG)
            if waited:
                status = child_status
                break
        if status is None:
            os.kill(pid, signal.SIGINT)
            _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
