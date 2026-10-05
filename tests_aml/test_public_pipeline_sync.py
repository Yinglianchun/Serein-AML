"""Public omission retention and opt-in Track selection survive the AML seam."""
import asyncio
import json

import pytest

from aml import engine, runtime
from serein.compat.raw_archive import raw_archive
from serein.core.store import Store
from serein.deployment import read_settings
from serein.extensions import pipeline
from test_engine import memory, role_output, add


def test_native_add_retains_omitted_scope_and_finishes_independent_scope(memory, monkeypatch):
    paths = engine._paths("user-a")
    settings = runtime.bootstrap(paths)
    archive = raw_archive(settings)
    archive.ingest([{"source_event_id": f"{session}-{role}", "session_id": session,
        "conversation_id": engine._user_key("user-a"), "role": role, "text": text,
        "created_at": f"2024-01-01T00:0{minute}:00Z"}
        for minute, session, role, text in (
            (0, "held", "user", "Retain these original details."),
            (1, "held", "assistant", "Those details remain important."),
            (2, "ready", "user", "Alice moved to Paris."),
            (3, "ready", "assistant", "You moved to Paris."))], source="serein_aml")
    monkeypatch.setattr(pipeline, "advance", pipeline.advance.original)
    current, calls = {}, []
    stage = runtime.run_stage

    async def run_stage(settings, role, request):
        current.update(role=role, request=request)
        return await stage(settings, role, request)

    async def complete(model, payload, **kwargs):
        role, request = current["role"], current["request"]
        calls.append(role)
        output = role_output(role, request)
        if role == "event_curator" and request["component"]["messages"][0]["original_session_id"] == "held":
            output["events"][0]["owned_unit_roots"].pop()
        return {"choices": [{"message": {"content": json.dumps(output)}}]}

    monkeypatch.setattr(runtime, "run_stage", run_stage)
    monkeypatch.setattr("serein.model_runtime.complete", complete)
    add([{"role": "user", "content": "An incomplete new tail."}])
    assert calls.count("event_writer") == 1
    assert calls.count("event_curator") == 3  # Public targeted repair, then the independent scope.
    with Store(paths.database, read_only=True) as store:
        assert store.conn.execute("SELECT count(*) FROM documents WHERE kind='event'").fetchone()[0] == 1
        assert {row[0] for row in store.conn.execute("SELECT raw_id FROM raw_processing")} == {3, 4}
        receipt = store.conn.execute("SELECT status,result_json FROM aml_add_receipts").fetchone()
        assert receipt["status"] == "complete"
        results = json.loads(receipt["result_json"])["pipeline"]
        assert results[0]["curator_omission_deferrals"][0]["deferred_source_message_ids"] == [1, 2]
        assert results[-1]["status"] == "current"
        paid_replies = store.conn.execute("SELECT output_text FROM pipeline_attempts WHERE error LIKE 'curator_coverage_pending_host:%'").fetchall()
        assert len(paid_replies) == 2 and all(json.loads(row[0])["events"] for row in paid_replies)
    monkeypatch.setattr(engine, "_rewrite_query", lambda *_: {"queries": ["Retain"], "entities": []})
    rows = engine.search_memory(query="Retain", options=None, user_id="user-a", top_k=4)
    assert any(row["id"] == "raw:1" for row in rows)
    before = len(calls)
    add([{"role": "user", "content": "An incomplete new tail."}])
    assert len(calls) == before  # Completed receipt retries do not repeat paid tasks.


def test_held_scope_is_drained_before_add_reports_remaining_pause(memory, monkeypatch):
    settings = runtime.bootstrap(engine._paths("user-a"))
    pipeline.initialize(settings.database)
    with Store(settings.database) as store:
        store.conn.execute("INSERT INTO pipeline_batches(id,scope,input_json,status) VALUES ('held','test','{}','paused_failure')")
    outcomes = [{"status": "paused", "job_id": "held:curator", "batch_id": "held"},
                {"status": "processed", "batch_id": "independent", "processed_originals": 2},
                {"status": "current"}]
    async def advance(*args, **kwargs):
        return outcomes.pop(0)
    monkeypatch.setattr(pipeline, "advance", advance)
    with pytest.raises(RuntimeError, match="paused batch"):
        asyncio.run(runtime.ingest_pipeline(settings))
    assert outcomes == []


def test_repeated_pause_cannot_loop_or_succeed(memory, monkeypatch):
    settings = runtime.bootstrap(engine._paths("user-a"))
    async def advance(*args, **kwargs):
        return {"status": "paused", "job_id": "held:curator", "batch_id": "held"}
    monkeypatch.setattr(pipeline, "advance", advance)
    with pytest.raises(RuntimeError, match="repeated a paused"):
        asyncio.run(runtime.ingest_pipeline(settings))


def test_ordinary_deferred_tail_still_stops_without_repeated_tasks(memory, monkeypatch):
    settings = runtime.bootstrap(engine._paths("user-a"))
    calls = []
    async def advance(*args, **kwargs):
        calls.append(1)
        return {"status": "processed", "batch_id": "tail", "processed_originals": 0}
    monkeypatch.setattr(pipeline, "advance", advance)
    assert len(asyncio.run(runtime.ingest_pipeline(settings))) == len(calls) == 1


def test_aml_bootstrap_does_not_enable_track_candidate_filter(memory):
    settings = runtime.bootstrap(engine._paths("user-a"))
    assert read_settings(settings.database)["pipeline"]["track_candidates_enabled"] is False
