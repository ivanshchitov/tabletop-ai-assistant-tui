"""Интеграция локального корпуса правил с запуском TUI."""

import shutil
from pathlib import Path

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
    session.send_line("Сколько ресурсов стоит дорога в CATAN?")
    session.wait_on_screen("Ответ на основе правил CATAN.")
    session.wait_on_screen("Источники правил:")
    session.wait_on_screen("Catan Rulebook")
    session.wait_on_screen("catan-rules.pdf:structural:")
    request_content = "\n".join(
        message["content"] for message in stub.requests[1]["payload"]["messages"]
    )
    assert "To build" in request_content

    session.send_line("/exit")
    session.wait_exit()

    assert stub.call_count == 2
