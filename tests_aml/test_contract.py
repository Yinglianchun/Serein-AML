from fastapi.testclient import TestClient

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
