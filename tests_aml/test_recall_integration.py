import asyncio
import json

import pytest

import aml.engine as engine
from serein.application import Application, Services
from serein.api.mcp import create_server
from serein.api.read_text import recall_text
from serein.config import Settings
from serein.core.store import Store
from serein.deployment import save_settings
from serein.recall.index import build_index, refresh_index


@pytest.fixture
def memories(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(engine, "_rewrite_query", lambda *_: {"queries": ["Dana"], "entities": ["Dana"]})
    monkeypatch.setattr(engine, "_EXPAND_ARCS", True)
    paths = engine._paths("alice")
    with Store(paths.database) as store:
        store.create("event_seed", "event", "Dana bought equipment", "Dana bought AsterBridge equipment.",
                     manual_surface=True)
        source = store.add_source("original", "Dana bought AsterBridge equipment.")
        store.bind("event_seed", source)
        store.create("event_factory", "event", "Equipment factory", "The equipment was produced in CopperBay.",
                     manual_surface=True)
        store.create("scene_hobby", "scene", "Weekend gardening", "UNRELATED_GARDENING_MATERIAL")
        store.create("narrative_equipment", "narrative", "Equipment history", "WHOLE_VOLUME_SHOULD_NOT_BE_INJECTED",
                     metadata={"arc_key": "equipment"})
        for kind, target in (("event", "event_seed"), ("event", "event_factory"), ("scene", "scene_hobby")):
            store.conn.execute("INSERT INTO narrative_materials VALUES ('narrative_equipment',1,'test',?,?, 'linked','{}')",
                               (kind, target))
    build_index(paths.database, paths.index)
    return paths


def search(**changes):
    return engine.search_memory(**{"query": "Where was Dana's equipment produced?", "options": None,
                                   "user_id": "alice", "top_k": 100, **changes})


def select_factory(prompt):
    menus = json.loads(prompt.split("MENUS:\n", 1)[1])
    assert "WHOLE_VOLUME_SHOULD_NOT_BE_INJECTED" not in prompt
    assert "UNRELATED_GARDENING_MATERIAL" not in prompt
    return {"selections": [{"arc_key": menu["arc_key"], "picks": [item["index"]]}
                           for menu in menus for item in menu["materials"] if item["id"] == "event_factory"]}


def test_reads_only_chosen_material_from_menu_and_preserves_user_boundary(memories, monkeypatch):
    monkeypatch.setattr(engine, "_model_json", select_factory)
    data = search()
    assert {row["id"] for row in data} == {"event_seed", "event_factory"}
    assert any("produced in CopperBay" in row["content"] for row in data)
    assert all("WHOLE_VOLUME" not in row["content"] and "UNRELATED" not in row["content"] for row in data)
    assert search(user_id="bob") == []
    assert len(search(top_k=1)) == 1


@pytest.mark.parametrize("choice", [[], [{"arc_key": "other-user-arc", "picks": [0]}],
                                   [{"arc_key": "equipment", "picks": [-1, 999, True]}]])
def test_irrelevant_or_invalid_selection_does_not_expand(memories, monkeypatch, choice):
    monkeypatch.setattr(engine, "_model_json", lambda _: {"selections": choice})
    assert [row["id"] for row in search()] == ["event_seed"]


def test_arc_expansion_is_opt_in(memories, monkeypatch):
    monkeypatch.setattr(engine, "_EXPAND_ARCS", False)
    monkeypatch.setattr(engine, "_model_json", lambda _: pytest.fail("Menu model should not run"))
    assert [row["id"] for row in search()] == ["event_seed"]


@pytest.mark.parametrize('registry', [False, True])
def test_collecting_arc_offers_materials_but_not_empty_narrative_body(memories, monkeypatch, registry):
    with Store(memories.database) as store:
        path, value = ('$.legacy_registry', '{"publication_status":"collecting","arc_key":"equipment"}') if registry else ('$.publication_status', '"collecting"')
        store.conn.execute("UPDATE revisions SET metadata_json=json_set(metadata_json,?,json(?)) "
                           "WHERE document_id='narrative_equipment'", (path, value))
    def choose(prompt):
        menus = json.loads(prompt.split("MENUS:\n", 1)[1])
        assert all(item['index'] != 0 for menu in menus for item in menu['materials'])
        return select_factory(prompt)
    monkeypatch.setattr(engine, '_model_json', choose)
    assert {row['id'] for row in search()} == {'event_seed', 'event_factory'}


def test_public_recall_domain_rules_are_used_instead_of_raw_fts(memories, monkeypatch):
    monkeypatch.setattr(engine, "_EXPAND_ARCS", False)
    with Store(memories.database) as store:
        doc = store.read("event_seed")
        store.revise("event_seed", expected_revision=doc["revision"], title=doc["title"], body_md=doc["body_md"],
                     metadata={"domain": "work"})
    refresh_index(memories.database, memories.index, ["event_seed"])
    save_settings(memories.database, {"recall": {"domains": {"work": "excluded"}}})
    assert search() == []


def test_total_content_budget_caps_selected_materials(memories, monkeypatch):
    with Store(memories.database) as store:
        doc = store.read("event_factory")
        store.revise("event_factory", expected_revision=doc["revision"], title=doc["title"],
                     body_md=doc["body_md"] + "\nLong source record." * 500)
    refresh_index(memories.database, memories.index, ["event_factory"])
    monkeypatch.setattr(engine, "_model_json", select_factory)
    monkeypatch.setattr(engine, "_CONTEXT_CHAR_CAP", 1000)
    data = search()
    assert {row["id"] for row in data} == {"event_seed", "event_factory"}
    assert sum(len(row["content"]) for row in data) == 1000
    assert any("produced in CopperBay" in row["content"] for row in data)


def test_changed_menu_is_not_returned_as_the_old_selection(memories, monkeypatch):
    monkeypatch.setattr(engine, "_model_json", select_factory)
    original = Services.arc_picks

    def changed(self, *args, **kwargs):
        return {**original(self, *args, **kwargs), "menu_fingerprint": "changed"}

    monkeypatch.setattr(Services, "arc_picks", changed)
    assert [row["id"] for row in search()] == ["event_seed"]


def test_mcp_and_automatic_render_share_card_format_but_have_different_envelopes(memories):
    settings = Settings(memories.database, memories.index)
    services = Services(settings)
    result = services.recall("Dana bought equipment", mode="surface", method="lexical", with_evidence=True)
    assert result["cards"] and not result["injected"]
    assert "[typed_memory ref=event:event_seed]" in result["context"]
    assert "[arc_materials key=equipment]" in result["context"]
    assert "[Serein Gateway Full Recall]" in result["full_additional_context"]
    server = create_server(Application(settings))
    blocks = asyncio.run(server.call_tool("recall_memory", {
        "query": "Dana bought equipment", "mode": "surface", "method": "lexical", "with_evidence": True}))
    text = "\n".join(block.text for block in blocks if block.type == "text")
    assert text == recall_text(result, with_evidence=True)
    assert result["context"] in text
    assert "[memory_details ref=event:event_seed]" in text
    assert "[Serein Gateway Full Recall]" not in text
