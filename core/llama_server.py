"""Запуск и завершение локального llama-server вместе с терминальным агентом."""

import os
from pathlib import Path
import signal
import subprocess
import time
from urllib.parse import urlsplit

import requests

from . import config


class LlamaServer:
    def __init__(
        self,
        script: Path = config.BASE_DIR / "llama_server" / "server.sh",
        base_url: str = "http://127.0.0.1:9999",
        log_path: Path = config.BASE_DIR / "llama_server" / "server.log",
        timeout: float | None = None,
    ):
        self.script = script
        self.base_url = base_url
        self.log_path = log_path
        self.timeout = timeout if timeout is not None else float(os.getenv("TABLETOP_LLAMA_START_TIMEOUT", "120"))
        self.process: subprocess.Popen | None = None
        self._existing_pid: int | None = None

    def _listener_pid(self) -> int | None:
        port = urlsplit(self.base_url).port
        try:
            result = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                capture_output=True, text=True, timeout=5,
            )
            pids = set(result.stdout.split())
            if not pids:
                if result.returncode not in (0, 1):
                    raise RuntimeError("Не удалось проверить слушателя локального порта.")
                return None
            if len(pids) != 1:
                raise RuntimeError(f"Порт {port} занят несколькими процессами, не llama-server.")
            pid = int(pids.pop())
            command = subprocess.run(
                ["ps", "-p", str(pid), "-o", "comm="],
                capture_output=True, text=True, timeout=5, check=True,
            ).stdout.strip()
            if Path(command).name != "llama-server":
                raise RuntimeError(f"Порт {port} занят процессом не llama-server; он не будет остановлен.")
            return pid
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise RuntimeError(f"Не удалось проверить llama-server: {exc}") from exc

    def _ready(self) -> bool:
        try:
            response = requests.get(self.base_url + "/health", timeout=1)
            return response.status_code == 200 and response.json().get("status") == "ok"
        except (requests.RequestException, ValueError):
            return False

    def start(self) -> None:
        if os.getenv("TABLETOP_LLAMA_AUTOSTART", "1") == "0":
            return
        try:
            self._existing_pid = self._listener_pid()
            if self._existing_pid is None:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                with self.log_path.open("w", encoding="utf-8") as log:
                    self.process = subprocess.Popen(
                        ["bash", str(self.script)], stdout=log, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL, start_new_session=True,
                    )
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if self.process is not None and self.process.poll() is not None:
                    detail = self.log_path.read_text(encoding="utf-8", errors="replace")[-1200:]
                    raise RuntimeError(f"llama-server завершился при запуске. {detail.strip()}")
                if self._ready():
                    return
                time.sleep(0.1)
            raise RuntimeError(f"Не дождались готовности llama-server за {self.timeout:g} с. Лог: {self.log_path}")
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        if self.process is not None:
            # У router-сервера есть дочерние модели; группа сохраняется, даже если
            # родитель уже упал. Завершаем всю созданную нами группу.
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=5)
        elif self._existing_pid is not None:
            pid, self._existing_pid = self._existing_pid, None
            # Проверка перед сигналом защищает от повторного использования PID.
            try:
                current_pid = self._listener_pid()
            except RuntimeError:
                return  # порт сменил владельца; чужой процесс не трогаем
            if current_pid != pid:
                return
            try:
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    state = subprocess.run(
                        ["ps", "-p", str(pid), "-o", "stat="],
                        capture_output=True, text=True, timeout=2,
                    ).stdout.strip()
                    if not state or state.startswith("Z"):
                        return
                    time.sleep(0.1)
                # Сервер мог перестать слушать порт, но остаться в процессе выхода.
                command = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "comm="],
                    capture_output=True, text=True, timeout=2,
                ).stdout.strip()
                if Path(command).name == "llama-server":
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
