"""Интеграция локального корпуса правил с запуском TUI."""

import json
import re
import shutil
from pathlib import Path

from core import rules_index

from .stub_api import answer


def test_rules_index_and_retrieval_work_only_after_explicit_indexing(
    app, stub, rules_documents_dir, rules_index_file
):
    rules_documents_dir.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "docs" / "rules" / "catan-rules.pdf"
    shutil.copyfile(source, rules_documents_dir / source.name)

    session = app()
    session.wait_for_prompt()
    assert not rules_index_file.exists()

    session.send_line("/rules")
    session.wait_on_screen("Индекс правил: не создан")
    session.wait_on_screen("Документы: 1 PDF")
    assert not rules_index_file.exists()

    session.send_line("/rules index")
    session.wait_on_screen("Индекс готов: 1 файла, 16 страниц текста")
    assert rules_index_file.exists()
    assert stub.call_count == 0

    session.send_line("/rules compare сколько ресурсов стоит дорога в CATAN")
    session.wait_on_screen("Фиксированный размер")
    session.wait_on_screen("По разделам")
    session.wait_on_screen("catan-rules.pdf")
    assert stub.call_count == 0

    stub.always(answer("Ответ на основе правил CATAN."))
    session.send_line("/rules mode off")
    session.wait_on_screen("Режим RAG: выключен")
    session.send_line("Сколько ресурсов стоит дорога в CATAN?")
    session.wait_on_screen("Ответ на основе правил CATAN.")
    assert "Источники правил:" not in session.read_for(0.1)
    without_rag = "\n".join(
        message["content"] for message in stub.requests[0]["payload"]["messages"]
    )
    assert "To build" not in without_rag

    session.send_line("/clear")
    session.wait_on_screen("История диалога очищена")
    session.send_line("/rules mode on")
    session.wait_on_screen("Режим RAG: включён")
    # Новый контракт: RAG по умолчанию использует rewrite и модельный второй этап.
    query = "CATAN road building resource cost"
    candidates = rules_index.search(rules_index_file, query, limit=20, minimum_score=-1.0)
    assert any("To build" in candidate.text for candidate in candidates)
    ratings = {"results": [
        {
            "id": number,
            "score": 0.9 if "To build" in candidate.text else 0.1,
            "reason": "Building rule" if "To build" in candidate.text else "Not the requested rule",
        }
        for number, candidate in enumerate(candidates, 1)
    ]}
    stub.sequence(
        answer(json.dumps({"query": query})),
        answer(json.dumps(ratings)),
        answer("Ответ на основе правил CATAN."),
    )
    session.send_line("Сколько ресурсов стоит дорога в CATAN?")
    session.wait_on_screen("Ответ на основе правил CATAN.")
    session.wait_on_screen("Источники правил:")
    session.wait_on_screen("Catan Rulebook")
    session.wait_on_screen("catan-rules.pdf:structural:")
    request_content = "\n".join(
        message["content"] for message in stub.requests[3]["payload"]["messages"]
    )
    assert "To build" in request_content

    session.send_line("/exit")
    session.wait_exit()

    assert stub.call_count == 4  # один ответ без RAG, затем rewrite + rerank + ответ


def test_rules_controls_trace_and_reranking_use_actual_terminal_and_http(
    app, stub, rules_documents_dir, rules_index_file, history_file
):
    rules_documents_dir.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "docs" / "rules" / "catan-rules.pdf"
    shutil.copyfile(source, rules_documents_dir / source.name)
    rules_index.build_index(rules_documents_dir, rules_index_file)
    session = app(rows=50)
    session.wait_for_prompt()
    session.send_line("/rules trace")
    session.wait_on_screen("Поиска правил ещё не было")
    session.send_line("/rules retrieval baseline")
    session.wait_on_screen("Режим поиска: baseline")
    session.send_line("/rules tune before=4 after=1 threshold=0.6")
    session.wait_on_screen("Кандидаты: 4; итоговый top-K: 1; порог: 0.6")
    assert stub.call_count == 0

    stub.sequence(answer("Базовый ответ: кирпич и дерево."))
    question = "Сколько ресурсов стоит дорога в CATAN?"
    session.send_line(question)
    session.wait_on_screen("Базовый ответ: кирпич и дерево.")
    session.send_line("/rules trace")
    session.wait_on_screen("Итог: фрагментов — 1; кандидатов — 4")
    assert stub.call_count == 1
    session.send_line("/clear")
    session.wait_on_screen("История диалога очищена")
    session.send_line("/rules retrieval enhanced")
    session.wait_on_screen("Режим поиска: enhanced")

    query = "CATAN road resources [/dim]"
    candidates = rules_index.search(rules_index_file, query, limit=4, minimum_score=-1.0)
    stub.sequence(
        answer(json.dumps({"query": query})),
        answer(json.dumps({"results": [
            {"id": 1, "score": 0.1, "reason": "Wrong topic [/dim]"},
            {"id": 2, "score": 0.9, "reason": "Road cost"},
            {"id": 3, "score": 0.8, "reason": "Related cost"},
            {"id": 4, "score": 0.6, "reason": "Related building"},
        ]})),
        answer("Улучшенный ответ: кирпич и дерево."),
    )
    session.send_line(question)
    session.wait_on_screen("Улучшенный ответ: кирпич и дерево.")
    session.send_line("/rules trace")
    session.wait_on_screen("Wrong topic [/dim]")
    session.wait_on_screen("ниже порога")
    session.wait_on_screen("за top-K")
    session.wait_on_screen("CATAN road resources [/dim]")
    session.wait_on_screen("Итог: фрагментов — 1; кандидатов — 4")
    context = "\n".join(
        message["content"] for message in stub.requests[3]["payload"]["messages"]
    )
    assert re.search(re.escape(candidates[1].chunk_id) + r"(?!\d)", context)
    assert not re.search(re.escape(candidates[0].chunk_id) + r"(?!\d)", context)
    assert not re.search(re.escape(candidates[2].chunk_id) + r"(?!\d)", context)
    assert question in context
    assert query not in context

    session.send_line("/rules tune before=2 after=3")
    session.wait_on_screen("Настройки поиска не изменены")
    session.send_line("/rules tune before=6 after=2 threshold=0.95")
    session.wait_on_screen("Кандидаты: 6; итоговый top-K: 2; порог: 0.95")
    session.send_line("/rules trace")
    session.wait_on_screen("Кандидаты: 4; итоговый top-K: 1; порог: 0.6")
    session.send_line("/rules")
    session.wait_on_screen("Кандидаты: 6; итоговый top-K: 2; порог: 0.95")
    session.send_line("/exit")
    session.wait_exit()

    assert stub.call_count == 4
    records = json.loads(history_file.read_text())["dialogues"]
    assert [(row["question"], row["answer"]) for row in records] == [
        (question, "Улучшенный ответ: кирпич и дерево.")
    ]
