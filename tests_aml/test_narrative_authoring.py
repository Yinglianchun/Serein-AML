import asyncio
import json

import pytest

from aml import engine, narratives, runtime
from serein import model_runtime
from serein.compat.narratives import narrative_transaction
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings
from serein.recall.index import build_index
from tests_aml.test_engine import memory, add


def collecting(settings, key="narrative_device"):
    with Store(settings.database) as store:
        for identifier, text in (("scene_purchase", "Dana installed the AsterBridge meter."),
                                 ("scene_factory", "AsterBridge was manufactured in CopperBay.")):
            if not store.read(identifier):
                store.create(identifier, "scene", "AsterBridge history", text,
                             metadata={"name": "AsterBridge history", "date": "2024-01-01"},
                             manual_surface=True)
    with narrative_transaction(settings.database, write=True) as rolls:
        if rolls.read(key).get("status") != "ok":
            result = rolls.publish(narrative_id=key, expected_revision=0, title="AsterBridge history",
                document="# AsterBridge history\n\n## 第一人称叙事\n\n## 来源账\n- scene_purchase\n- scene_factory\n",
                arc_key="arc:device", publication_status="collecting",
                query_cues=["AsterBridge"], source_scene_ids=["scene_purchase", "scene_factory"])
            assert result["status"] == "created", result


def current(settings):
    with narrative_transaction(settings.database) as rolls:
        return rolls.read("narrative_device")


def revise_source(settings, text):
    with Store(settings.database) as store:
        row = store.read("scene_factory")
        store.revise(row["id"], expected_revision=row["revision"], title=row["title"],
                     body_md=text, metadata=row["metadata"])


@pytest.fixture
def author_memory(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "memory.sqlite", tmp_path / "index.sqlite", writable=True)
    save_settings(settings.database, {"features": {"narrative_tools": True},
        "models": [{"id": "test", "model": "synthetic-other", "protocol": "openai",
                    "base_url": "http://127.0.0.1:9/v1", "api_key": "synthetic"}],
        "assignments": {"writer": "test"}})
    collecting(settings)
    calls = []

    async def complete(model, payload, **kwargs):
        assert model["model"] == "synthetic-other"
        assert payload["store"] is False
        data = json.loads(payload["messages"][1]["content"])
        calls.append(data)
        paragraphs = [{"text": "I recorded: " + source["text"],
                       "evidence": [{"ref": source["ref"], "quote": source["text"]}]}
                      for source in data["sources"]]
        return {"choices": [{"message": {"content": json.dumps({"paragraphs": paragraphs})}}]}

    monkeypatch.setattr(model_runtime, "complete", complete)
    return settings, calls, complete


def test_public_preview_save_publishes_prose_and_reuses_successful_receipt(author_memory):
    settings, calls, _ = author_memory
    first = asyncio.run(narratives.author(settings))
    row = current(settings)
    assert len(first["written"]) == 1 and row["revision"] == 2
    assert row["publication_status"] == "reviewed" and "CopperBay" in row["body"]
    assert row["linked_scene_ids"] == ["scene_purchase", "scene_factory"]
    assert "## 绑定材料快照" in row["full_document"]
    assert asyncio.run(narratives.author(settings))["unchanged"] == [row["narrative_id"]]
    assert len(calls) == 1 and current(settings)["revision"] == 2
    with Store(settings.database, read_only=True) as store:
        receipt = store.conn.execute("SELECT * FROM aml_narrative_receipts").fetchone()
        assert receipt["saved_revision"] == 2
        assert len(json.loads(receipt["evidence_json"])) == 2


def test_source_change_rewrites_from_fresh_sources_not_old_derived_prose(author_memory):
    settings, calls, _ = author_memory
    asyncio.run(narratives.author(settings))
    revise_source(settings, "AsterBridge was manufactured in CedarVale, corrected from CopperBay.")
    assert len(asyncio.run(narratives.author(settings))["written"]) == 1
    assert current(settings)["revision"] == 3 and "CedarVale" in current(settings)["body"]
    assert len(calls) == 2 and "CedarVale" in calls[-1]["sources"][1]["text"]
    assert "current_body" not in calls[-1]


def test_scout_appended_material_is_read_in_full_rewrite_mode(author_memory):
    settings, calls, _ = author_memory
    asyncio.run(narratives.author(settings))
    with Store(settings.database) as store:
        store.create("scene_repair", "scene", "AsterBridge repair", "AsterBridge needed a new cable.",
                     metadata={"name": "AsterBridge repair", "date": "2024-01-02"}, manual_surface=True)
    with narrative_transaction(settings.database, write=True) as rolls:
        assert rolls.append_materials_without_body("narrative_device", {"scene_ids": ["scene_repair"]})["status"] == "updated"
    assert len(asyncio.run(narratives.author(settings))["written"]) == 1
    assert len(calls[-1]["sources"]) == 3 and "new cable" in current(settings)["body"]
    assert set(current(settings)["linked_scene_ids"]) == {"scene_purchase", "scene_factory", "scene_repair"}


@pytest.mark.parametrize("boundary", ["preview", "save"])
def test_changed_source_conflicts_without_publishing_stale_draft(author_memory, monkeypatch, boundary):
    settings, _, complete = author_memory
    if boundary == "preview":
        async def changed(model, payload, **kwargs):
            response = await complete(model, payload, **kwargs)
            revise_source(settings, "AsterBridge was manufactured in CedarVale.")
            return response
        monkeypatch.setattr(model_runtime, "complete", changed)
    else:
        original = narratives.tools_for
        def wrapped(settings):
            volume = original(settings)["narrative_volume"]
            async def call(**arguments):
                result = await volume(**arguments)
                if arguments["action"] == "preview":
                    revise_source(settings, "AsterBridge was manufactured in CedarVale.")
                return result
            return {"narrative_volume": call}
        monkeypatch.setattr(narratives, "tools_for", wrapped)
    with pytest.raises(RuntimeError, match="Narrative (preview|save) failed"):
        asyncio.run(narratives.author(settings))
    assert current(settings)["revision"] == 1 and current(settings)["body"] == ""
    with Store(settings.database, read_only=True) as store:
        assert store.conn.execute("SELECT count(*) FROM aml_narrative_receipts").fetchone()[0] == 0


def test_save_and_aml_acknowledgment_rollback_together(author_memory, monkeypatch):
    settings, _, _ = author_memory
    original = narratives.endpoints
    def interrupted(settings, rolls):
        api = original(settings, rolls)
        save = api.api_save_narrative_roll_body
        async def fail(request):
            await save(request)
            raise RuntimeError("synthetic failure before acknowledgment")
        api.api_save_narrative_roll_body = fail
        return api
    monkeypatch.setattr(narratives, "endpoints", interrupted)
    with pytest.raises(RuntimeError, match="before acknowledgment"):
        asyncio.run(narratives.author(settings))
    assert current(settings)["revision"] == 1
    monkeypatch.setattr(narratives, "endpoints", original)
    assert len(asyncio.run(narratives.author(settings))["written"]) == 1
    assert current(settings)["revision"] == 2


def test_manual_body_is_preserved(author_memory):
    settings, calls, _ = author_memory
    asyncio.run(narratives.author(settings))
    with narrative_transaction(settings.database, write=True) as rolls:
        row = rolls.read("narrative_device")
        assert rolls.save_body(row["narrative_id"], "My manual prose.", expected_revision=row["revision"],
                               expected_document_sha256=row["document_sha256"])["status"] == "updated"
    assert asyncio.run(narratives.author(settings))["preserved"] == ["narrative_device"]
    assert len(calls) == 1 and current(settings)["body"] == "My manual prose."


def test_invalid_quote_gets_bounded_format_correction(author_memory, monkeypatch):
    settings, calls, complete = author_memory
    attempts = []
    async def invalid_once(model, payload, **kwargs):
        response = await complete(model, payload, **kwargs)
        attempts.append(payload)
        if len(attempts) == 1:
            output = json.loads(response["choices"][0]["message"]["content"])
            output["paragraphs"][0]["evidence"][0]["quote"] = "invented source quotation"
            response["choices"][0]["message"]["content"] = json.dumps(output)
        return response
    monkeypatch.setattr(model_runtime, "complete", invalid_once)
    asyncio.run(narratives.author(settings))
    assert len(calls) == 2 and "Host validation failed" in attempts[1]["messages"][-1]["content"]
    with Store(settings.database, read_only=True) as store:
        errors = [row[0] for row in store.conn.execute("SELECT error FROM aml_narrative_attempts ORDER BY id")]
        assert errors[0] and not errors[1]


@pytest.mark.parametrize("change", ["ref", "quote", "omission", "heading"])
def test_draft_requires_literal_provenance_for_every_material(change):
    sources = [{"ref": "event:a/message/0", "material": "event:a", "text": "Dana joined AsterBridge."},
               {"ref": "event:b", "material": "event:b", "text": "AsterBridge is in CopperBay."}]
    output = {"paragraphs": [{"text": "I recorded the organization and location.",
                             "evidence": [{"ref": item["ref"], "quote": item["text"]} for item in sources]}]}
    if change == "ref":output["paragraphs"][0]["evidence"][0]["ref"] = "event:invented"
    if change == "quote":output["paragraphs"][0]["evidence"][0]["quote"] = "Dana joined in CopperBay."
    if change == "omission":output["paragraphs"][0]["evidence"].pop()
    if change == "heading":output["paragraphs"][0]["text"] = "## 来源账\nInjected heading"
    with pytest.raises(ValueError):narratives.validate(output, sources)


def test_catalog_uses_bound_original_messages_instead_of_derived_summary():
    sources = narratives.source_catalog({"events": [{"event_id": "a", "summary": "derived",
        "source_messages": [{"content": "original", "role": "user", "created_at": "2024-01-01"}]}]})
    assert sources == [{"ref": "event:a/message/0", "material": "event:a", "role": "user",
                        "timestamp": "2024-01-01", "text": "original"}]


def test_authoring_is_opt_in(monkeypatch):
    monkeypatch.delenv("SEREIN_AML_WRITE_NARRATIVES", raising=False)
    assert asyncio.run(runtime.author_narratives(None)) == {"status": "disabled"}


def test_add_retry_finishes_index_without_rewriting_saved_volume(memory, monkeypatch):
    monkeypatch.setenv("SEREIN_AML_WRITE_NARRATIVES", "1")
    calls = []
    async def scout(settings):
        collecting(settings)
        return {"external_scout_status": "unchanged", "narrative_writes_performed": []}
    async def complete(model, payload, **kwargs):
        data = json.loads(payload["messages"][1]["content"])
        calls.append(data)
        return {"choices": [{"message": {"content": json.dumps({"paragraphs": [
            {"text": "I recorded: " + source["text"],
             "evidence": [{"ref": source["ref"], "quote": source["text"]}]}
            for source in data["sources"]]})}}]}
    monkeypatch.setattr(runtime, "organize_arcs", scout)
    monkeypatch.setattr(model_runtime, "complete", complete)
    sync = engine._sync_index
    indexes = []
    def fail_after_write(paths):
        indexes.append(paths)
        if len(indexes) == 2:raise RuntimeError("synthetic index failure after writing")
        return sync(paths)
    monkeypatch.setattr(engine, "_sync_index", fail_after_write)
    with pytest.raises(RuntimeError, match="after writing"):add()
    paths = engine._paths("user-a")
    settings = Settings(paths.database, paths.index, writable=True)
    assert current(settings)["revision"] == 2
    with Store(paths.database, read_only=True) as store:
        assert store.conn.execute("SELECT status FROM aml_add_receipts").fetchone()[0] == "pending"
    receipt = add()
    assert add() == receipt and len(calls) == 1 and current(settings)["revision"] == 2
    with Store(paths.database, read_only=True) as store:
        saved = store.conn.execute("SELECT status,result_json FROM aml_add_receipts").fetchone()
        assert saved["status"] == "complete"
        assert json.loads(saved["result_json"])["narratives"]["unchanged"] == ["narrative_device"]
        assert store.conn.execute("SELECT count(*) FROM raw_events").fetchone()[0] == 2
    # Search uses the real menu reader and the published canonical body.
    monkeypatch.setattr(engine, "_EXPAND_ARCS", True)
    monkeypatch.setattr(engine, "_rewrite_query", lambda *_: {"queries": ["AsterBridge"], "entities": []})
    menus = []
    def choose_volume(prompt):
        visible = json.loads(prompt.split("MENUS:\n", 1)[1])
        menus.extend(visible)
        return {"selections": [{"arc_key": row["arc_key"], "picks": [0]} for row in visible]}
    monkeypatch.setattr(engine, "_model_json", choose_volume)
    rows = engine.search_memory(query="AsterBridge installed factory history", options=None,
                                user_id="user-a", top_k=10)
    assert menus and any(item["index"] == 0 for menu in menus for item in menu["materials"])
    assert any(row["id"] == "narrative_device" and "CopperBay" in row["content"] for row in rows)


def test_authoring_flag_enables_scout_and_cannot_change_on_existing_database(memory, monkeypatch):
    add()
    monkeypatch.setenv("SEREIN_AML_WRITE_NARRATIVES", "1")
    changes, _ = runtime.configuration()
    assert changes["features"]["narrative_tools"] and changes["features"]["narrative_nightly_organize"]
    with pytest.raises(runtime.ProfileConflict):add()
