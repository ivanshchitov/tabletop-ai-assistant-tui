"""Записанные ответы настоящей модели для e2e-сценариев.

Выдуманные ответы в stub-сервере проверяют механику, но не то, как приложение ведёт себя с
реальным текстом `deepseek-v4-flash` — его манерой, разметкой и случаями, когда он не уложился
в заданный формат. Кассета записывается один раз обращением к живому OpenCode Zen и дальше
проигрывается локально.

Записать или обновить (нужен настоящий OPENCODE_API_KEY, тратит квоту):

    python3 -m pytest tests/e2e/test_recorded_answers.py -m network --record-cassettes
"""

import json
from pathlib import Path
from typing import Dict, List

CASSETTE_DIR = Path(__file__).parent / "cassettes"

# Вопросы, покрывающие все ветки поведения модели: обычный вопрос по правилам,
# запрос-подборка (проверяет лимит списка), общее описание игры (карточка/JSON-схема)
# и заведомо посторонний вопрос (обязан вернуть фразу отказа).
RECORDED_QUESTIONS: Dict[str, str] = {
    "rules": "Как считаются очки за монастырь в Каркассоне?",
    "recommendation": "Что посоветуешь для компании из четырёх человек?",
    "description": "Расскажи про Catan",
    "off_topic": "Какая погода завтра в Москве?",
}

REFUSAL_PHRASE = (
    "Я не могу рассказать об этом, потому что я — Tabletop AI Assistant, и я работаю только с "
    "вопросами по настольным играм. Пожалуйста, задайте вопрос по теме."
)


def cassette_path(fmt: str) -> Path:
    return CASSETTE_DIR / f"{fmt}.json"


def load(fmt: str) -> Dict[str, str]:
    path = cassette_path(fmt)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save(fmt: str, answers: Dict[str, str]) -> None:
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    cassette_path(fmt).write_text(
        json.dumps(answers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def available_formats() -> List[str]:
    return sorted(p.stem for p in CASSETTE_DIR.glob("*.json"))
