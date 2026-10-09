"""Запуск собственного процесса модели и сетевого шлюза."""

import argparse
import logging
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from dotenv import load_dotenv

from .backend import Backend
from .http_server import ServiceServer
from .policy import CONTEXT_SIZE, MODEL, Policy


class ModelProcess:
    def __init__(self, *, port=10099, executable="llama-server", model_path=None, start_timeout=120):
        self.port = port
        self.executable = executable
        self.model_path = model_path
        self.start_timeout = start_timeout
        self.process = None
        self._stopped = False
        self.backend = Backend(f"http://127.0.0.1:{port}")

    def start(self):
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", self.port))
        except OSError as exc:
            raise RuntimeError("Внутренний порт модели занят или недоступен; чужой процесс не остановлен.") from exc
        command = [self.executable, "--host", "127.0.0.1", "--port", str(self.port),
                   "--alias", MODEL, "--ctx-size", str(CONTEXT_SIZE), "--parallel", "1",
                   "--jinja", "--no-context-shift", "--reasoning", "auto", "--reasoning-budget", "256",
                   "--temp", "0.3", "--top-p", "0.95", "--top-k", "20", "--min-p", "0.05",
                   "--flash-attn", "off", "--n-gpu-layers", "999", "--threads", "6",
                   "--threads-batch", "6", "--batch-size", "256", "--ubatch-size", "64"]
        command += (["--model", str(self.model_path)] if self.model_path
                    else ["--hf-repo", "unsloth/Qwen3.5-2B-GGUF:Q4_K_M"])
        # Ключи нужны только шлюзу; модель не получает секреты клиента или облака.
        env = {key: value for key, value in os.environ.items()
               if key not in {"TABLETOP_SERVICE_API_KEY", "TABLETOP_LOCAL_API_KEY", "OPENCODE_API_KEY", "LLAMA_API_KEY"}}
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            start_new_session=True, env=env)
            deadline = time.monotonic() + self.start_timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("llama-server завершился при запуске; проверьте модель и совместимость CLI.")
                if self.backend.ready():
                    return
                time.sleep(0.1)
            raise RuntimeError("Истекло время ожидания готовности модели.")
        except BaseException:
            self.stop()
            raise

    def stop(self):
        if self.process is None or self._stopped:
            return
        try:
            os.killpg(self.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait(timeout=5)
        finally:
            # Повторный вызов не должен сигналить группе с переиспользованным PID.
            self._stopped = True


def _port(value):
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("Порт должен быть от 1 до 65535.")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description="Приватный HTTP-сервис Qwen3.5-2B.")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес шлюза; для сети задайте LAN-адрес сервера.")
    parser.add_argument("--port", type=_port, default=8080, help="Порт HTTP API (8080).")
    parser.add_argument("--backend-port", type=_port, default=10099, help="Внутренний loopback-порт модели.")
    parser.add_argument("--llama-command", default="llama-server", help="Путь к llama-server.")
    parser.add_argument("--model-file", type=Path, help="Путь к GGUF; по умолчанию Qwen3.5-2B Q4_K_M из HF-кэша.")
    parser.add_argument("--start-timeout", type=float, default=120, help="Ожидание загрузки модели, секунды.")
    args = parser.parse_args(argv)
    try:
        policy = Policy(os.getenv("TABLETOP_SERVICE_API_KEY", ""))
        if args.port == args.backend_port or not 0 < args.start_timeout < 3600:
            raise ValueError("Нужны разные порты и таймаут от 0 до 3600 секунд.")
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    model = ModelProcess(port=args.backend_port, executable=args.llama_command,
                         model_path=args.model_file, start_timeout=args.start_timeout)
    previous = {}
    server = None

    def terminate(signum, frame):
        raise KeyboardInterrupt

    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, terminate)
        model.start()
        server = ServiceServer((args.host, args.port), policy, model.backend)
        print(f"Приватный сервис готов: {args.host}:{args.port}; контекст 8192, ответ ≤1024, 10 запросов/мин.", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        print("Сервис остановлен.", flush=True)
    except (OSError, RuntimeError) as exc:
        print(f"Не удалось запустить сервис: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            if server is not None:
                server.server_close()
        finally:
            model.stop()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    return 0


def entry_point():
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    return main()
