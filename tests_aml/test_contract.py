from fastapi.testclient import TestClient
import pytest

import aml.app as api


client = TestClient(api.app)


def test_health_is_unauthenticated():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_add_echoes_contract_ids(monkeypatch):
    monkeypatch.setattr(api, "add_memory", lambda **kwargs: "event_test")
    payload = {
        "request_id": "req-1",
        "messages": [{"role": "user", "timestamp": 1704067200000, "content": "hello"}],
        "user_id": "user-1",
        "session_id": "session-1",
    }
    response = client.post("/add", json=payload)
    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "request_id": "req-1",
        "user_id": "user-1",
        "session_id": "session-1",
    }


def test_search_has_data_wrapper_and_respects_shape(monkeypatch):
    monkeypatch.setattr(
        api,
        "search_memory",
        lambda **kwargs: [{
            "id": "mem-1",
            "content": "evidence",
            "score": 0.5,
            "created_at": "2026-09-20T00:00:00Z",
        }],
    )
    response = client.post("/search", json={
        "query": "What happened?",
        "options": ["A", "B"],
        "user_id": "user-1",
        "top_k": 100,
    })
    assert response.status_code == 200
    assert response.json() == {"data": [{
        "id": "mem-1",
        "content": "evidence",
        "score": 0.5,
        "created_at": "2026-09-20T00:00:00Z",
    }]}


def test_search_rejects_top_k_over_100():
    response = client.post("/search", json={
        "query": "What happened?",
        "user_id": "user-1",
        "top_k": 101,
    })
    assert response.status_code == 422


@pytest.mark.parametrize('failure', [ValueError, RuntimeError])
def test_search_internal_error_is_not_contract_error(monkeypatch, caplog, failure):
    def fail(**kwargs):
        raise failure('private-question-and-upstream-body')
    monkeypatch.setattr(api, 'search_memory', fail)
    response = client.post('/search', json={
        'query': 'private-query', 'user_id': 'private-user', 'top_k': 5,
    })
    assert response.status_code == 500
    detail = response.json()['detail']
    assert detail['reason'] == 'Search failed internally'
    assert detail['error_id'] in caplog.text
    assert failure.__name__ in caplog.text
    assert 'test_contract.py:fail:' in caplog.text
    assert 'private-' not in caplog.text + response.text


def test_search_validation_logs_only_field_and_type(caplog):
    response = client.post('/search', json={
        'query': 'private-query', 'user_id': 'private-user',
        'options': ['private-option'], 'top_k': 'private-invalid-value',
    })
    assert response.status_code == 422
    assert 'top_k' in caplog.text and 'int_parsing' in caplog.text
    assert 'private-' not in caplog.text


def test_search_input_budget_still_returns_422(monkeypatch, caplog):
    def fail(**kwargs):
        raise api.SearchInputError('AML Search question/options exceed the input budget')
    monkeypatch.setattr(api, 'search_memory', fail)
    response = client.post('/search', json={
        'query': 'synthetic', 'user_id': 'test', 'top_k': 5,
    })
    assert response.status_code == 422
    assert 'reason=input_budget' in caplog.text


def test_bad_internal_result_is_not_request_validation(monkeypatch, caplog):
    monkeypatch.setattr(api, 'search_memory', lambda **kwargs: [{'content': 'private-memory'}])
    response = client.post('/search', json={'query': 'synthetic', 'user_id': 'test', 'top_k': 5})
    assert response.status_code == 500
    assert 'private-memory' not in caplog.text + response.text
