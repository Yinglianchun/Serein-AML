"""Arc material choices survive ranking only while their evidence remains valid."""

import json

import pytest

import aml.engine as engine
from aml import bridges, originals
from serein.adapters.reranker import RerankerClient
from serein.application import Services
from serein.core.store import Store
from serein.deployment import save_settings
from serein.recall.index import build_index, refresh_index


@pytest.fixture
def bridge_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(engine, "_EXPAND_ARCS", True)
    monkeypatch.setattr(engine, "_EXPAND_ENTITIES", False, raising=False)
    monkeypatch.setattr(engine, "_RETURN_CAP", 40)
    monkeypatch.setattr(engine, "_rewrite_query", lambda *_: {"queries": ["Dana equipment"], "entities": []})
    monkeypatch.setattr(originals, "hits", lambda *_, **kwargs: [])

    paths = engine._paths("synthetic-user")
    with Store(paths.database) as store:
        store.create("event_anchor", "event", "Dana equipment",
                     "Dana bought the AsterBridge device; its factory was discussed separately.",
                     manual_surface=True)
        store.create("event_bridge", "event", "Factory location",
                     "The AsterBridge device was made in CopperBay.", manual_surface=True)
        store.create("narrative_equipment", "narrative", "Device history", "",
                     metadata={"arc_key": "equipment", "publication_status": "collecting"})
        for target in ("event_anchor", "event_bridge"):
            store.conn.execute(
                "INSERT INTO narrative_materials VALUES ('narrative_equipment',1,'test','event',?,'linked','{}')",
                (target,))
        for index in range(45):
            store.create(f"event_noise_{index:02d}", "event", "Dana equipment detail",
                         f"Dana asked about equipment delivery and production paperwork item {index}.",
                         manual_surface=True)
    build_index(paths.database, paths.index)
    save_settings(paths.database, {"models": [{"id": "mock-rerank", "model": "mock-rerank",
        "base_url": "https://mock.invalid/v1", "protocol": "openai", "api_key": "synthetic"}],
        "assignments": {"reranker": "mock-rerank"}})

    observed = {"menus": [], "rerank_ids": []}
    recall = engine._recall_hits

    def anchor_first(services, text, **kwargs):
        # Admission still uses real Services.recall. Stable seed order ensures
        # the anchor's Arc is among the six menus even when 45 noise hits match.
        hits = recall(services, text, **kwargs)
        return sorted(hits, key=lambda hit: hit["id"] != "event_anchor")

    def choose_bridge(prompt):
        menus = json.loads(prompt.split("MENUS:\n", 1)[1])
        observed["menus"].extend(menus)
        return {"selections": [{"arc_key": menu["arc_key"], "picks": [item["index"]]}
                               for menu in menus for item in menu["materials"]
                               if item["id"] == "event_bridge"]}

    def rerank(self, query, documents):
        observed["rerank_ids"] = [item["ref"] for item in documents]
        return {item["ref"]: (0.99 if item["ref"] == "event_anchor" else
                              0.01 if item["ref"] == "event_bridge" else 0.8)
                for item in documents}

    monkeypatch.setattr(engine, "_recall_hits", anchor_first)
    monkeypatch.setattr(engine, "_model_json", choose_bridge)
    monkeypatch.setattr(RerankerClient, "__call__", rerank)
    return paths, observed


def search(**changes):
    return engine.search_memory(**{"query": "Where was Dana's equipment produced?",
                                   "options": None, "user_id": "synthetic-user", "top_k": 100, **changes})


def test_chosen_bridge_survives_noise_and_respects_result_caps(bridge_memory):
    _, observed = bridge_memory
    rows = search()
    assert observed["menus"]
    assert "event_bridge" in observed["rerank_ids"]
    assert sum(key.startswith("event_noise_") for key in observed["rerank_ids"]) >= 40
    assert len(rows) == 40
    assert {"event_anchor", "event_bridge"} <= {row["id"] for row in rows}
    assert all(set(row) == {"id", "content", "score", "created_at"} for row in rows)

    two = search(top_k=2)
    assert {row["id"] for row in two} == {"event_anchor", "event_bridge"}
    one = search(top_k=1)
    assert len(one) <= 1


@pytest.mark.parametrize("rule", ["excluded", "explicit_only"])
def test_arc_pick_does_not_bypass_domain_rule(bridge_memory, monkeypatch, rule):
    paths, _ = bridge_memory
    monkeypatch.setattr(engine, "_RETURN_CAP", 100)
    with Store(paths.database) as store:
        doc = store.read("event_bridge")
        store.revise(doc["id"], expected_revision=doc["revision"], title=doc["title"],
                     body_md=doc["body_md"], metadata={**doc["metadata"], "domain": "work"})
    refresh_index(paths.database, paths.index, ["event_bridge"])
    save_settings(paths.database, {"recall": {"domains": {"work": rule}}})
    assert "event_bridge" not in {row["id"] for row in search()}


def test_retired_arc_material_is_not_reserved(bridge_memory, monkeypatch):
    paths, _ = bridge_memory
    monkeypatch.setattr(engine, "_RETURN_CAP", 100)
    with Store(paths.database) as store:
        store.set_lifecycle("event_bridge", "archived")
    refresh_index(paths.database, paths.index, ["event_bridge"])
    assert "event_bridge" not in {row["id"] for row in search()}


@pytest.mark.parametrize("change", ["fingerprint", "revision"])
def test_changed_arc_pick_is_not_reserved(bridge_memory, monkeypatch, change):
    paths, _ = bridge_memory
    monkeypatch.setattr(engine, "_RETURN_CAP", 100)
    original = Services.arc_picks

    def changed(self, *args, **kwargs):
        page = original(self, *args, **kwargs)
        if change == "fingerprint":
            return {**page, "menu_fingerprint": "changed-after-menu"}
        with Store(paths.database) as store:
            doc = store.read("event_bridge")
            store.revise(doc["id"], expected_revision=doc["revision"], title=doc["title"],
                         body_md=doc["body_md"] + " Updated after the menu read.")
        return page

    monkeypatch.setattr(Services, "arc_picks", changed)
    assert "event_bridge" not in {row["id"] for row in search()}


def test_late_menu_change_cannot_fall_back_to_flat_ranking(bridge_memory, monkeypatch):
    paths, _ = bridge_memory
    original = Services.arc_picks

    def change_menu_after_pick(self, *args, **kwargs):
        page = original(self, *args, **kwargs)
        with Store(paths.database) as store:
            doc = store.read("narrative_equipment")
            store.revise(doc["id"], expected_revision=doc["revision"],
                         title="Changed device history", body_md=doc["body_md"], metadata=doc["metadata"])
        return page

    def rank_bridge_high(self, query, documents):
        return {item["ref"]: (0.99 if item["ref"] == "event_anchor" else
                              0.98 if item["ref"] == "event_bridge" else 0.8)
                for item in documents}

    monkeypatch.setattr(Services, "arc_picks", change_menu_after_pick)
    monkeypatch.setattr(RerankerClient, "__call__", rank_bridge_high)
    assert "event_bridge" not in {row["id"] for row in search(top_k=2)}


def test_reserved_pair_survives_long_leading_whitespace(bridge_memory, monkeypatch):
    paths, _ = bridge_memory
    monkeypatch.setattr(engine, "_CONTEXT_CHAR_CAP", 1000)
    with Store(paths.database) as store:
        doc = store.read("event_anchor")
        store.revise(doc["id"], expected_revision=doc["revision"], title=doc["title"],
                     body_md=" " * 1200 + doc["body_md"], metadata=doc["metadata"])
    refresh_index(paths.database, paths.index, ["event_anchor"])
    rows = search(top_k=2)
    assert {row["id"] for row in rows} == {"event_anchor", "event_bridge"}
    assert all(row["content"].strip() for row in rows)


def test_explicit_arc_pair_precedes_automatic_entity_pair_when_two_slots(bridge_memory, monkeypatch):
    paths, _ = bridge_memory
    with Store(paths.database) as store:
        store.create("event_auto", "event", "Other factory",
                     "AsterBridge factory was based in Northport.", manual_surface=True)
    refresh_index(paths.database, paths.index, ["event_auto"])
    monkeypatch.setattr(engine, "_EXPAND_ENTITIES", True)
    seen = []
    select = bridges.select

    def capture(records, ordered, groups, limit):
        seen.extend(groups)
        return select(records, ordered, groups, limit)

    monkeypatch.setattr(bridges, "select", capture)
    rows = search(top_k=2)
    assert any(group["route"] == "entity_relation" and group["ids"][-1] == "event_auto" for group in seen)
    assert any(group["route"] == "arc_menu" and group["ids"][-1] == "event_bridge" for group in seen)
    assert {row["id"] for row in rows} == {"event_anchor", "event_bridge"}
