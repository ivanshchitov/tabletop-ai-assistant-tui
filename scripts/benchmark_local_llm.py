#!/usr/bin/env python3
"""День 29: одинаковые вопросы, отдельный offline-сервер, полные ответы и RSS.

Запуск: .venv/bin/python scripts/benchmark_local_llm.py --output docs/local-llm-day29-results.json
"""

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import config, local_llm
from core.answer_settings import AnswerSettings
from core.api_client import APIClient, APIError
from core.history_manager import HistoryManager
from core.long_term_memory import LongTermMemory
from core.schedule_store import ScheduleStore
from core.tabletop_agent import TabletopAgent
from core.task_state import TaskStore
from core.user_profile import ProfileStore


QUESTIONS = [
    {"id": "knight", "question": "Как ходит конь в шахматах? Ответь в двух предложениях.",
     "criterion": "Две клетки по одной оси и одна по другой; может перепрыгивать фигуры."},
    {"id": "catan-cost", "question": "Какие ресурсы нужны для двух дорог и одного поселения в базовой CATAN? Посчитай суммарно, кратко.",
     "criterion": "3 древесины, 3 глины/кирпича, 1 шерсть, 1 зерно. Дорога: дерево+кирпич; поселение: дерево+кирпич+шерсть+зерно."},
    {"id": "dice", "question": "В CATAN поселение A получает 1 ресурс при сумме 6, B — 2 ресурса при сумме 3. Где выше средний доход за бросок двух обычных кубиков? Кратко посчитай.",
     "criterion": "A: 5/36; B: 2*2/36=4/36. A выше по среднему доходу."},
    {"id": "bishop", "question": "Как ходит слон в шахматах? Может ли он перепрыгивать фигуры?",
     "criterion": "По диагонали на любое число свободных клеток; перепрыгивать нельзя."},
    {"id": "ambiguous", "question": "Сколько карт можно взять за ход в настольной игре?",
     "criterion": "Уточнить название игры или правило, не выдавать число как универсальное."},
    {"id": "off-topic", "question": "Напиши рецепт борща.",
     "criterion": "Точная фраза отказа Tabletop AI Assistant; без рецепта."},
]
VARIANTS = [local_llm.MODELS["Q4_K_M"][0], local_llm.MODELS["Q4_K_M"][1],
            local_llm.MODELS["Q4_K_XL"][1]]


class RecordingClient(APIClient):
    def __init__(self, seed):
        super().__init__("", seed=seed, cache_prompt=False)
        self.records = []

    def _request(self, messages, max_tokens, temperature, model):
        data, elapsed = super()._request(messages, max_tokens, temperature, model)
        meta = self._meta_from(data, elapsed, model)
        self.records.append({**asdict(meta), "messages": messages,
                             "temperature": temperature, "max_tokens": max_tokens})
        return data, elapsed


def run_question(folder, model, question, seed):
    """Настоящий агент с полностью изолированной памятью, без RAG и инструментов."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    paths = {key: folder / (key + ".json") for key in ("history", "memory", "profile", "task", "schedule")}
    client = RecordingClient(seed)
    settings = AnswerSettings(max_words=70, temperature=0.7)
    agent = TabletopAgent(client, settings=settings, model=model,
        history=HistoryManager(paths["history"]), long_term=LongTermMemory(paths["memory"]),
        profile=ProfileStore(paths["profile"]), task=TaskStore(paths["task"]),
        schedule=ScheduleStore(paths["schedule"]), task_results_dir=folder / "tasks")
    agent.config.auto_tools = False
    agent.rag_enabled = False
    agent.reset()
    parameters = asdict(agent.local_report())
    started = time.perf_counter()
    answer, error, status = "", None, "ok"
    try:
        meta = agent.ask(question["question"])
        answer = meta.content
        if any(record["finish_reason"] == "length" for record in client.records):
            status = "length"
        elif not answer:
            status = "empty"
        elif agent.last_invariants and agent.last_invariants.rejected:
            status = "rejected"
    except APIError as exc:
        error, status = str(exc), "error"
    return {
        **question, "model": model, "seed": seed, "parameters": parameters,
        "answer": answer, "status": status, "error": error,
        "wall_seconds": time.perf_counter() - started,
        "elapsed_seconds": sum(record["elapsed_seconds"] for record in client.records),
        "prompt_tokens": sum(record["prompt_tokens"] for record in client.records),
        "completion_tokens": sum(record["completion_tokens"] for record in client.records),
        "http_requests": client.records,
        "state_paths": {key: str(path) for key, path in paths.items()},
        "quality": None,
    }


def summarize(records):
    measured = [record for record in records if record["status"] != "error" and record["elapsed_seconds"] > 0]
    seconds = sum(record["elapsed_seconds"] for record in measured)
    tokens = sum(record["completion_tokens"] for record in measured)
    return {
        "requests": len(records),
        "complete_answers": sum(record["status"] == "ok" for record in records),
        "errors": sum(record["status"] == "error" for record in records),
        "truncated": sum(record["status"] == "length" for record in records),
        "median_seconds": statistics.median(record["elapsed_seconds"] for record in measured) if measured else None,
        "tokens_per_second": tokens / seconds if seconds > 0 and tokens > 0 else None,
        "prompt_tokens": sum(record["prompt_tokens"] for record in records),
        "completion_tokens": sum(record["completion_tokens"] for record in records),
    }


def group_rss_kib(output, group):
    values = []
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 3 and int(fields[1]) == group:
            values.append(int(fields[2]))
    return sum(values) if values else None


class BenchmarkServer:
    """Владеет только созданной группой процессов; существующий сервер не трогает."""
    def __init__(self, folder):
        self.folder = Path(folder)
        self.process = None
        self.log = None
        self.peak_rss_kib = None
        self.rss_error = None
        self._stop = threading.Event()
        self._thread = None

    def _sample(self):
        while not self._stop.is_set():
            try:
                result = subprocess.run(["ps", "-axo", "pid=,pgid=,rss="],
                                        capture_output=True, text=True, timeout=3, check=True)
                value = group_rss_kib(result.stdout, self.process.pid)
                if value is not None:
                    self.peak_rss_kib = max(self.peak_rss_kib or 0, value)
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                self.rss_error = str(exc)
                return
            self._stop.wait(0.2)

    def start(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        # Загрузка измеряется первым прогревом выбранного пресета, не исходной модели.
        preset = self.folder / "models.ini"
        preset.write_text(config.LLAMA_MODELS_FILE.read_text().replace("load-on-startup = true", "load-on-startup = false"))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.log = (self.folder / "server.log").open("w")
        self.process = subprocess.Popen(["llama-server", "--host", "127.0.0.1", "--port", str(port),
            "--models-max", "1", "--models-preset", str(preset), "--offline", "--no-webui"],
            stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError((self.folder / "server.log").read_text()[-3000:])
            try:
                if requests.get(self.url + "/health", timeout=1).status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(0.1)
        raise RuntimeError("Сервер сравнения не запустился за 120 секунд")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=4)
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=5)
        if self.log:
            self.log.close()


def save(report, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "docs/local-llm-day29-results.json")
    parser.add_argument("--seeds", type=int, nargs="+", default=[42])
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=VARIANTS)
    parser.add_argument("--questions", nargs="+", choices=[q["id"] for q in QUESTIONS], default=["knight", "catan-cost", "dice"])
    args = parser.parse_args()
    version = subprocess.run(["llama-server", "--version"], capture_output=True, text=True, check=True)
    report = {"created_utc": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
        "llama_version": (version.stdout + version.stderr).strip(), "seeds": args.seeds, "max_words": 70,
        "request_timeout_seconds": 600, "max_retries": 1,
        "rag": False, "auto_tools": False, "cache_prompt": False,
        "rss_method": "Максимум суммы RSS группы процессов сервера, выборка каждые 0.2 с; не полная память устройства или энергия.",
        "variants": []}
    selected = [q for q in QUESTIONS if not args.questions or q["id"] in args.questions]
    report["question_ids"] = [q["id"] for q in selected]
    runtime = ROOT / "videos/tmp/day29-benchmark"
    old_url = os.environ.get("TABLETOP_LOCAL_API_URL")
    old_timeout = config.REQUEST_TIMEOUT
    old_retries = config.MAX_RETRIES
    config.REQUEST_TIMEOUT = 600
    config.MAX_RETRIES = 1
    try:
        for index, model in enumerate(args.variants):
            folder = runtime / f"{int(time.time())}-{index}"
            server = BenchmarkServer(folder)
            variant = {"model": model, "records": [], "peak_rss_kib": None}
            report["variants"].append(variant)
            print(f"Загрузка: {model}", flush=True)
            try:
                server.start()
                os.environ["TABLETOP_LOCAL_API_URL"] = server.url + "/v1/chat/completions"
                models = requests.get(server.url + "/models", timeout=10).json()["data"]
                info = next(item for item in models if item["id"] == model)
                variant["model_metadata"] = info
                path = info.get("path")
                variant["weights_bytes"] = Path(path).stat().st_size if path and Path(path).is_file() else None
                with tempfile.TemporaryDirectory(prefix="day29-") as state:
                    started = time.perf_counter()
                    warmup_client = RecordingClient(42)
                    warmup = warmup_client.ask_with_usage_messages([
                        {"role": "system", "content": "Ты помощник по настольным играм."},
                        {"role": "user", "content": "Назови одну настольную игру."},
                    ], max_tokens=32, temperature=0.7, model=model)
                    variant["load_and_warmup_seconds"] = time.perf_counter() - started
                    variant["warmup"] = warmup_client.records[0]
                    variant["server_props"] = requests.get(server.url + "/props", params={"model": model}, timeout=10).json()
                    actual_context = variant["server_props"].get("default_generation_settings", {}).get("n_ctx")
                    expected_context = local_llm.report(model, AnswerSettings()).context_window
                    if actual_context != expected_context:
                        raise RuntimeError(f"Окно сервера {actual_context}, ожидалось {expected_context}")
                    # У установленного router /models не содержит path; /props содержит
                    # фактический GGUF после загрузки, в том числе для именованного пресета.
                    path = variant["server_props"].get("model_path")
                    variant["weights_bytes"] = Path(path).stat().st_size if path and Path(path).is_file() else None
                    variant["warmup_peak_rss_kib"] = server.peak_rss_kib
                    server.peak_rss_kib = None
                    save(report, args.output)
                    for seed in args.seeds:
                        for question in selected:
                            record = run_question(Path(state) / f"{seed}-{question['id']}", model, question, seed)
                            variant["records"].append(record)
                            variant["summary"] = summarize(variant["records"])
                            variant["peak_rss_kib"] = server.peak_rss_kib
                            variant["rss_error"] = server.rss_error
                            save(report, args.output)
                            print(f"{model} seed={seed} {question['id']}: {record['status']}, "
                                  f"{record['elapsed_seconds']:.2f} с, {record['completion_tokens']} ток.", flush=True)
            finally:
                variant["peak_rss_kib"] = server.peak_rss_kib
                variant["rss_error"] = server.rss_error
                server.stop()
                save(report, args.output)
    finally:
        config.REQUEST_TIMEOUT = old_timeout
        config.MAX_RETRIES = old_retries
        if old_url is None:
            os.environ.pop("TABLETOP_LOCAL_API_URL", None)
        else:
            os.environ["TABLETOP_LOCAL_API_URL"] = old_url
    print(f"Результаты: {args.output}", flush=True)


if __name__ == "__main__":
    def interrupt(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupt)
    main()
