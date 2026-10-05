"""Published volume prose is admitted only while its sources remain eligible."""

import asyncio
import json

import pytest

from aml import engine, narratives, originals
from serein.compat.narratives import narrative_transaction
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings
from serein.recall.index import build_index


@pytest.fixture
def published_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(engine, "_EXPAND_ARCS", True)
    monkeypatch.setattr(engine, "_EXPAND_ENTITIES", False)
    monkeypatch.setattr(engine, "_rewrite_query", lambda *_: {"queries": ["AsterBridge"], "entities": []})
    monkeypatch.setattr(originals, "hits", lambda *_, **__: [])

    paths = engine._paths("synthetic-user")
    settings = Settings(paths.database, paths.index, writable=True)
    with Store(paths.database) as store:
        store.create("scene_anchor", "scene", "AsterBridge history",
                     "Dana installed the AsterBridge meter.", manual_surface=True)
        store.create("scene_factory", "scene", "Factory location",
                     "The meter was manufactured in CopperBay.",
                     metadata={"domain": "work"}, manual_surface=True)
    with narrative_transaction(paths.database, write=True) as rolls:
        result = rolls.publish(narrative_id="narrative_device", expected_revision=0,
            title="AsterBridge history", arc_key="arc:device", publication_status="collecting",
            query_cues=["AsterBridge"], source_scene_ids=["scene_anchor", "scene_factory"],
            document="# AsterBridge history\n\n## 第一人称叙事\n\n"
                     "## 来源账\n- scene_anchor\n- scene_factory\n")
        assert result["status"] == "created"
    save_settings(paths.database, {"features": {"narrative_tools": True}})

    async def draft(_settings, _narrative, receipt, _stamp):
        sources = narratives.source_catalog(receipt["materials"])
        body = "\n\n".join("I recorded: " + source["text"] for source in sources)
        evidence = [{"text": "I recorded: " + source["text"],
                     "evidence": [{"ref": source["ref"], "quote": source["text"]}]}
                    for source in sources]
        return body, evidence

    monkeypatch.setattr(narratives, "draft", draft)
    assert len(asyncio.run(narratives.author(settings))["written"]) == 1
    build_index(paths.database, paths.index)

    def choose_volume(prompt):
        menus = json.loads(prompt.split("MENUS:\n", 1)[1])
        return {"selections": [{"arc_key": menu["arc_key"], "picks": [0]} for menu in menus
                               if any(item["index"] == 0 for item in menu["materials"])]}

    monkeypatch.setattr(engine, "_model_json", choose_volume)
    return paths, settings


def search(query="What is the AsterBridge history?"):
    return engine.search_memory(query=query, options=None,
                                user_id="synthetic-user", top_k=10)


def test_fresh_published_body_is_readable(published_volume):
    rows = search()
    assert any(row["id"] == "narrative_device" and "CopperBay" in row["content"] for row in rows)


@pytest.mark.parametrize("rule", ["excluded", "explicit_only"])
def test_bound_source_domain_rule_blocks_whole_body(published_volume, rule):
    paths, _ = published_volume
    save_settings(paths.database, {"recall": {"domains": {"work": rule}}})
    rows = search()
    assert "scene_anchor" in {row["id"] for row in rows}
    assert "narrative_device" not in {row["id"] for row in rows}


def test_explicitly_named_bound_source_allows_body(published_volume):
    paths, _ = published_volume
    save_settings(paths.database, {"recall": {"domains": {"work": "explicit_only"}}})
    assert "narrative_device" in {row["id"] for row in search(
        "Where does Factory location fit in AsterBridge history?")}


def test_source_revision_blocks_old_body_before_next_author_pass(published_volume):
    paths, settings = published_volume
    with Store(paths.database) as store:
        row = store.read("scene_factory")
        store.revise(row["id"], expected_revision=row["revision"], title=row["title"],
                     body_md="The meter was manufactured in CedarVale.", metadata=row["metadata"])
    rows = search()
    assert "scene_anchor" in {row["id"] for row in rows}
    assert "narrative_device" not in {row["id"] for row in rows}
    assert len(asyncio.run(narratives.author(settings))["written"]) == 1
    engine._sync_index(paths)
    assert any(row["id"] == "narrative_device" and "CedarVale" in row["content"]
               for row in search())


def test_appended_material_blocks_old_body_before_next_author_pass(published_volume):
    paths, _ = published_volume
    with Store(paths.database) as store:
        store.create("scene_repair", "scene", "Cable repair", "The meter needed a new cable.",
                     manual_surface=True)
    with narrative_transaction(paths.database, write=True) as rolls:
        assert rolls.append_materials_without_body("narrative_device", {
            "scene_ids": ["scene_repair"]})["status"] == "updated"
    rows = search()
    assert "scene_anchor" in {row["id"] for row in rows}
    assert "narrative_device" not in {row["id"] for row in rows}


def test_new_menu_link_blocks_old_body_without_volume_revision(published_volume):
    paths, _ = published_volume
    with Store(paths.database) as store:
        store.create("event_later", "event", "Later repair", "The meter needed a new cable.",
                     manual_surface=True)
        store.conn.execute("INSERT INTO event_arc_links VALUES (?,?,?,?)",
                           ("arc:device", "event_later", "synthetic", "{}"))
    rows = search()
    assert "scene_anchor" in {row["id"] for row in rows}
    assert "narrative_device" not in {row["id"] for row in rows}
