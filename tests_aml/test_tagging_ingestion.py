"""The AML Add route uses the public tagger and its current-source contracts."""
import asyncio
import json
import sqlite3

import pytest

from aml import engine, runtime, tagging
from serein import model_runtime
from serein.config import Settings
from serein.core.store import Store, digest
from serein.tagging_entities import current_entities
from tests_aml.test_engine import memory, add


def response(value):
    return {"choices": [{"message": {"content": json.dumps(value)}}]}


def named_output(request, name="Alice"):
    material = next(item for item in request["materials"] if name in item["text"])
    return {"domain": "life", "entities": [{"name": name, "type": "person",
        "supports": [{"source_id": material["source_id"], "quote": material["text"]}]}]}


@pytest.fixture
def enabled(memory, monkeypatch):
    monkeypatch.setenv("SEREIN_AML_TAG_MEMORIES", "1")
    calls = []

    async def complete(model, payload, **kwargs):
        assert model["model"] == "synthetic-other" and payload["store"] is False
        request = json.loads(payload["messages"][1]["content"])
        calls.append(request)
        return response(named_output(request))

    monkeypatch.setattr(model_runtime, "complete", complete)
    return calls


def test_add_tags_bound_originals_and_refreshes_entity_index(enabled):
    receipt = add()
    paths = engine._paths("user-a")
    assert len(enabled) == 1 and enabled[0]["kind"] == "event"
    with Store(paths.database) as store:
        row = store.read(store.conn.execute("SELECT id FROM documents WHERE kind='event'").fetchone()[0])
        entity = current_entities(store, row)[0]
        assert entity["name"] == "Alice" and entity["supports"][0]["kind"] == "bound_source"
        assert row["body_md"] == "Alice moved to Paris."
        saved = json.loads(store.conn.execute("SELECT result_json FROM aml_add_receipts WHERE id=?", (receipt,)).fetchone()[0])
        assert saved["tagging"]["status"] == "ok"
        assert saved["tagging"]["indexes"]["entities"]["model_validated"] == 1
    with sqlite3.connect(paths.index) as connection:
        assert connection.execute("SELECT text FROM entity_observations WHERE text='Alice'").fetchone()
    assert add() == receipt and len(enabled) == 1


def test_paid_failure_keeps_add_pending_then_explicit_retry_reuses_event(enabled, memory, monkeypatch):
    calls = []

    async def complete(model, payload, **kwargs):
        request = json.loads(payload["messages"][1]["content"])
        calls.append(request)
        return response({"domain": "life"} if len(calls) == 1 else named_output(request))

    monkeypatch.setattr(model_runtime, "complete", complete)
    with pytest.raises(RuntimeError, match="tagging did not complete"):
        add()
    paths = engine._paths("user-a")
    with Store(paths.database) as store:
        assert store.conn.execute("SELECT status FROM aml_add_receipts").fetchone()[0] == "pending"
        assert store.conn.execute("SELECT status,attempts FROM import_tag_jobs").fetchone()[:] == ("failed", 1)
    assert len(calls) == 1
    add()
    assert len(calls) == 2 and memory == ["track_router", "event_curator", "event_writer"]
    with Store(paths.database) as store:
        assert store.conn.execute("SELECT count(*) FROM raw_events").fetchone()[0] == 2
        assert store.conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
        assert store.conn.execute("SELECT status,attempts FROM import_tag_jobs").fetchone()[:] == ("done", 2)


def test_invalid_entity_quotes_are_filtered_without_inventing_replacements(enabled, monkeypatch):
    async def complete(model, payload, **kwargs):
        request = json.loads(payload["messages"][1]["content"])
        output = named_output(request)
        output["entities"].append({"name": "Invented", "type": "place", "supports": [
            {"source_id": request["materials"][0]["source_id"], "quote": "Invented"}]})
        return response(output)

    monkeypatch.setattr(model_runtime, "complete", complete)
    add()
    with Store(engine._paths("user-a").database) as store:
        row = store.read(store.conn.execute("SELECT id FROM documents").fetchone()[0])
        assert [item["name"] for item in current_entities(store, row)] == ["Alice"]
        assert row["metadata"]["entity_rejected_count"] == 1


def test_tagger_drains_more_than_one_public_pair_and_preserves_authored_fields(enabled):
    paths = engine._paths("user-a")
    settings = runtime.bootstrap(paths)
    with Store(paths.database) as store:
        for number in range(7):
            store.create(f"scene_{number}", "scene", "Visit", "Alice visited Paris.",
                metadata={"canonical_domain": "inner", "scene_cues": ["a visit"]}, manual_surface=True)
    result = asyncio.run(tagging.run(settings))
    assert result == {"status": "ok", "tagged": 7} and len(enabled) == 7
    with Store(paths.database) as store:
        for number in range(7):
            row = store.read(f"scene_{number}")
            assert row["body_md"] == "Alice visited Paris." and row["title"] == "Visit"
            assert row["metadata"]["canonical_domain"] == "inner"
            assert row["metadata"]["scene_cues"] == ["a visit"]
            assert current_entities(store, row)[0]["supports"][0]["kind"] == "memory_body"


def test_changed_binding_is_retagged_from_current_sources(enabled, monkeypatch):
    settings = runtime.bootstrap(engine._paths("user-a"))
    with Store(settings.database) as store:
        store.create("scene_1", "scene", "Visit", "Alice visited Paris.", manual_surface=True)
        source = store.add_source("synthetic/one", "Alice visited Paris.")
        binding = store.bind("scene_1", source)
    calls = []

    async def complete(model, payload, **kwargs):
        request = json.loads(payload["messages"][1]["content"])
        calls.append(request)
        if len(calls) == 1:
            with Store(settings.database) as store:
                store.unbind(binding)
        return response(named_output(request))

    monkeypatch.setattr(model_runtime, "complete", complete)
    assert asyncio.run(tagging.run(settings))["status"] == "ok"
    assert len(calls) == 2
    with Store(settings.database) as store:
        assert current_entities(store, store.read("scene_1"))[0]["supports"][0]["kind"] == "memory_body"


def test_archived_during_tagging_does_not_block_active_queue(enabled, monkeypatch):
    settings = runtime.bootstrap(engine._paths("user-a"))
    with Store(settings.database) as store:
        store.create("scene_1", "scene", "Visit", "Alice visited Paris.", manual_surface=True)

    async def complete(model, payload, **kwargs):
        request = json.loads(payload["messages"][1]["content"])
        with Store(settings.database) as store:
            store.conn.execute("UPDATE documents SET lifecycle='archived' WHERE id='scene_1'")
        return response(named_output(request))

    monkeypatch.setattr(model_runtime, "complete", complete)
    assert asyncio.run(tagging.run(settings)) == {"status": "ok", "tagged": 0}
    with Store(settings.database) as store:
        assert not current_entities(store, store.read("scene_1"))


def test_opted_in_import_scene_uses_public_cue_rules(enabled, monkeypatch):
    settings = runtime.bootstrap(engine._paths("user-a"))
    from serein.imports import initialize_imports
    initialize_imports(settings.database)
    with Store(settings.database) as store:
        store.create("scene_1", "scene", "Visit", "Alice visited Paris.",
                     metadata={"operit_original": {}}, manual_surface=True)
        store.conn.execute("INSERT INTO import_tag_jobs(document_id,body_hash,upload_id) VALUES (?,?,?)",
                           ("scene_1", digest("Alice visited Paris."), ""))
    calls = []

    async def complete(model, payload, **kwargs):
        request = json.loads(payload["messages"][1]["content"])
        calls.append(payload)
        return response({**named_output(request), "cues": ["a visit", "User visit"]})

    monkeypatch.setattr(model_runtime, "complete", complete)
    asyncio.run(tagging.run(settings))
    assert "额外返回 cues 数组" in calls[0]["messages"][0]["content"]
    with Store(settings.database) as store:
        assert store.read("scene_1")["metadata"]["scene_cues"] == ["a visit"]


def test_tagging_flag_cannot_silently_change_an_existing_profile(memory, monkeypatch):
    add()
    monkeypatch.setenv("SEREIN_AML_TAG_MEMORIES", "1")
    with pytest.raises(runtime.ProfileConflict):
        add()


def test_development_can_select_a_separate_tagger(memory, monkeypatch, tmp_path):
    filename = tmp_path / "models.json"
    filename.write_text(json.dumps({"models": [
        {"id": "writer", "model": "synthetic-writer", "base_url": "http://127.0.0.1:9/v1"},
        {"id": "tagger", "model": "synthetic-tagger", "base_url": "http://127.0.0.1:9/v1"}],
        "upstreams": [], "assignments": {"writer": "writer", "operit_tagging": "tagger"}}))
    monkeypatch.setenv("SEREIN_AML_MODEL_CONFIG", str(filename))
    changes, _ = runtime.configuration()
    assert changes["assignments"]["operit_tagging"] == "tagger"
    assert changes["assignments"]["event_writer"] == "writer"


def test_store_policy_stays_local_to_tagging_context(memory, monkeypatch):
    requests = []

    async def complete(model, payload, **kwargs):
        requests.append(payload)
        return response({"domain": None, "entities": []})

    monkeypatch.setattr(model_runtime, "complete", complete)
    tagging.transport_policy()

    async def calls():
        token = tagging._ACTIVE.set(True)
        try:
            await model_runtime.complete({}, {"label": "tagging"})
        finally:
            tagging._ACTIVE.reset(token)
        await model_runtime.complete({}, {"label": "other"})

    asyncio.run(calls())
    assert requests == [{"label": "tagging", "store": False}, {"label": "other"}]


def test_disabled_tagging_never_calls_a_provider(memory, monkeypatch):
    monkeypatch.delenv("SEREIN_AML_TAG_MEMORIES", raising=False)
    monkeypatch.setattr(model_runtime, "complete", lambda *_: pytest.fail("Tagging is opt-in"))
    assert asyncio.run(runtime.tag_memories(None)) == {"status": "disabled"}


def test_index_failure_retry_does_not_repeat_successful_tagging(enabled, monkeypatch):
    sync = engine._sync_index
    attempts = []

    def fail_once(paths):
        attempts.append(paths)
        if len(attempts) == 1:
            raise RuntimeError("synthetic index failure after tagging")
        return sync(paths)

    monkeypatch.setattr(engine, "_sync_index", fail_once)
    with pytest.raises(RuntimeError, match="after tagging"):
        add()
    assert len(enabled) == 1
    add()
    assert len(enabled) == 1
    with Store(engine._paths("user-a").database) as store:
        assert store.conn.execute("SELECT attempts FROM import_tag_jobs").fetchone()[0] == 1
        assert store.conn.execute("SELECT status FROM aml_add_receipts").fetchone()[0] == "complete"


def test_empty_grounded_entities_is_a_valid_completed_result(enabled, monkeypatch):
    async def complete(model, payload, **kwargs):
        return response({"domain": None, "entities": []})

    monkeypatch.setattr(model_runtime, "complete", complete)
    add()
    with Store(engine._paths("user-a").database) as store:
        row = store.read(store.conn.execute("SELECT id FROM documents").fetchone()[0])
        assert row["metadata"]["tagged_entities"] == []
        assert store.conn.execute("SELECT status FROM import_tag_jobs").fetchone()[0] == "done"


def test_index_refresh_keeps_public_cue_pause_status(memory, monkeypatch):
    paths = engine._paths("user-a")
    runtime.bootstrap(paths)
    engine._sync_index(paths)
    settings = Settings(paths.database, paths.index,
        embedding={"endpoint": "https://mock.invalid/embeddings"},
        background={"germany_config_file": "unused"}, recall={"germany_policy_file": "unused"})
    monkeypatch.setattr("serein.compat.entity_scope.update_scope", lambda _: [])
    calls = []

    async def refresh_public(value, **kwargs):
        calls.append(kwargs)
        return {"lexical": {"status": "ok"}, "cues": {
            "failed_scenes": ["scene_1"], "paused_scenes": ["scene_1"]}, "canonical_writes": 0}

    monkeypatch.setattr("serein.recall.legacy_indexes.refresh", refresh_public)
    result = asyncio.run(tagging.refresh(settings))
    assert result["legacy"]["cues"]["paused_scenes"] == ["scene_1"]
    assert calls == [{}]  # No forced retry bypasses the public binder's pause.
