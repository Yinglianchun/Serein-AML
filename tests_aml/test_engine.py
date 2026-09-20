import aml.engine as engine


def _notes(transcript):
    return {
        "title": "Alice moved",
        "summary": "Alice moved to Paris.",
        "facts": ["Alice moved to Paris."],
        "entities": ["Alice", "Paris"],
    }


def _plan(query, options):
    return {"queries": ["Alice Paris", "Alice"], "entities": ["Alice"]}


def test_add_then_search_uses_real_serein_store_and_index(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(engine, "_extract_notes", _notes)
    monkeypatch.setattr(engine, "_rewrite_query", _plan)

    engine.add_memory(
        request_id="req-1",
        messages=[{
            "role": "user",
            "timestamp": 1704067200000,
            "content": "Alice moved to Paris last spring.",
        }],
        user_id="user-a",
        session_id="session-1",
    )

    data = engine.search_memory(
        query="Where did Alice move?",
        options=None,
        user_id="user-a",
        top_k=100,
    )

    assert data
    assert data[0]["id"].startswith("event_aml_")
    assert "Alice moved to Paris last spring." in data[0]["content"]


def test_user_id_is_a_hard_retrieval_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(engine, "_extract_notes", _notes)
    monkeypatch.setattr(engine, "_rewrite_query", _plan)

    engine.add_memory(
        request_id="req-1",
        messages=[{"role": "user", "content": "Alice moved to Paris."}],
        user_id="user-a",
        session_id="session-1",
    )

    assert engine.search_memory(
        query="Alice",
        options=None,
        user_id="user-b",
        top_k=100,
    ) == []
