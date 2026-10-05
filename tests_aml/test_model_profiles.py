"""Academic model restrictions fail before any provider call or ingestion."""
import json

import pytest

from aml import runtime


@pytest.fixture
def profile(tmp_path, monkeypatch):
    filename = tmp_path / 'models.json'
    monkeypatch.setenv('SEREIN_AML_PROFILE', 'competition')
    monkeypatch.setenv('SEREIN_AML_MODEL_CONFIG', str(filename))
    monkeypatch.setenv('OR_key', 'synthetic-generation-key')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)
    source = {'models': [
        {'id':'embedding', 'model':'text-embedding-v4', 'protocol':'openai',
         'base_url':'https://embedding.invalid/v1', 'api_key':'synthetic-embedding-key'},
        {'id':'reranker', 'model':'synthetic-reranker', 'protocol':'openai',
         'base_url':'https://reranker.invalid/v1', 'api_key':'synthetic-reranker-key'},
        {'id':'writer', 'model':'synthetic-development', 'protocol':'openai',
         'base_url':'https://writer.invalid/v1'}],
        'upstreams': [], 'assignments': {'embedding':'embedding', 'reranker':'reranker', 'writer':'writer'}}
    def write():
        filename.write_text(json.dumps(source), encoding='utf-8')
    return source, write


@pytest.mark.parametrize('model', [None, 'Qwen/Qwen3-Embedding-4B', 'text-embedding-v3'])
def test_competition_rejects_missing_or_other_embeddings(profile, model):
    source, write = profile
    if model is None:
        source['assignments'].pop('embedding')
    else:
        source['models'][0]['model'] = model
    write()
    with pytest.raises(runtime.ProfileConflict, match='text-embedding-v4'):
        runtime.configuration()


def test_competition_rejects_incompatible_embedding_transport(profile):
    source, write = profile
    source['models'][0]['protocol'] = 'anthropic'
    write()
    with pytest.raises(runtime.ProfileConflict, match='compatible embeddings API'):
        runtime.configuration()


def test_competition_requires_explicit_model_config(profile, monkeypatch):
    monkeypatch.delenv('SEREIN_AML_MODEL_CONFIG', raising=False)
    with pytest.raises(runtime.ProfileConflict, match='SEREIN_AML_MODEL_CONFIG'):
        runtime.configuration()


def test_competition_keeps_v4_and_reranker_but_replaces_all_generation(profile):
    _, write = profile
    write()
    changes, marker = runtime.configuration()
    models = {m['id']:m for m in changes['models']}
    assert models[changes['assignments']['embedding']]['model'] == 'text-embedding-v4'
    assert models[changes['assignments']['reranker']]['model'] == 'synthetic-reranker'
    assert {changes['assignments'][role] for role in runtime.GENERATIVE_ROLES} == {'aml-mini'}
    assert 'writer' not in models
    assert 'synthetic-' not in str(marker)


def test_development_preserves_its_selected_embedding(profile, monkeypatch):
    source, write = profile
    source['models'][0]['model'] = 'Qwen/Qwen3-Embedding-4B'
    write()
    monkeypatch.setenv('SEREIN_AML_PROFILE', 'development')
    changes, _ = runtime.configuration()
    assert next(m for m in changes['models'] if m['id'] == 'embedding')['model'] == 'Qwen/Qwen3-Embedding-4B'
    assert {changes['assignments'][role] for role in runtime.GENERATIVE_ROLES} == {'writer'}
