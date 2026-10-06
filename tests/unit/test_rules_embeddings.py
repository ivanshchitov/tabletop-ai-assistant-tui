import json

import pytest
import requests
import responses

from core import config
from core.rules_index import RulesIndexError


def test_embedding_presets_are_excluded_from_chat(tmp_path):
    presets = tmp_path / "models.ini"
    presets.write_text("version=1\n[*]\n[chat]\n[embed]\nembedding=true ; local\n")
    assert config.local_models(presets) == ["chat"]
    assert config.local_embedding_models(presets) == ["embed"]
    assert config.local_embedding_models(tmp_path / "missing.ini") == []


@pytest.mark.parametrize("names", [[], ["one", "two"]])
def test_local_embedding_configuration_must_be_unambiguous(monkeypatch, names):
    from core import rules_embeddings
    monkeypatch.setattr(config, "LOCAL_EMBEDDING_MODELS", names)
    assert rules_embeddings.for_model(config.DEFAULT_MODEL) is None
    with pytest.raises(RulesIndexError, match="embedding"):
        rules_embeddings.for_model(config.LOCAL_MODELS[0])


@responses.activate
def test_local_embedding_http_contract_normalization_and_query_instruction(monkeypatch):
    from core import rules_embeddings
    monkeypatch.setenv("TABLETOP_LOCAL_API_URL", "http://localhost:1234/v1/chat/completions")
    monkeypatch.setattr(config, "LOCAL_EMBEDDING_MODELS", ["embed"])
    responses.post("http://localhost:1234/v1/embeddings", json={
        "data": [{"index": 0, "embedding": [3, 4]}], "model": "embed",
    })
    provider = rules_embeddings.for_model(config.LOCAL_MODELS[0])
    assert provider.embed("A road costs brick.") == pytest.approx([0.6, 0.8])
    assert provider.embed_query("Road cost?") == pytest.approx([0.6, 0.8])
    first, second = responses.calls
    assert json.loads(first.request.body) == {"model": "embed", "input": "A road costs brick."}
    assert "Road cost?" in json.loads(second.request.body)["input"]
    assert "Instruct:" in json.loads(second.request.body)["input"]
    assert "Authorization" not in first.request.headers
    assert first.request.req_kwargs["timeout"] > 0


@responses.activate
@pytest.mark.parametrize("payload", [
    {}, {"data": []}, {"data": [{"index": 1, "embedding": [1]}]},
    {"data": [{"index": 0, "embedding": []}]},
    {"data": [{"index": 0, "embedding": [0, 0]}]},
    {"data": [{"index": 0, "embedding": [True, 2]}]},
    {"data": [{"index": 0, "embedding": ["1", 2]}]},
    {"data": [{"index": 0, "embedding": [float("nan"), 2]}]},
])
def test_invalid_embeddings_are_user_facing_errors(payload):
    from core.rules_embeddings import LocalEmbeddings
    responses.post("http://localhost/v1/embeddings", json=payload)
    with pytest.raises(RulesIndexError):
        LocalEmbeddings("embed", "http://localhost/v1/embeddings").embed("text")


@responses.activate
@pytest.mark.parametrize("failure", [requests.Timeout("timeout"), requests.ConnectionError("offline")])
def test_embedding_transport_failure_is_user_facing(failure):
    from core.rules_embeddings import LocalEmbeddings
    responses.post("http://localhost/v1/embeddings", body=failure)
    with pytest.raises(RulesIndexError, match="embedding"):
        LocalEmbeddings("embed", "http://localhost/v1/embeddings").embed("text")
