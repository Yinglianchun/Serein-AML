"""Evidence sufficiency and joint bridge review preserve public Search guards."""
import json

import pytest

from aml import arc_planning, engine, originals
from serein.adapters.reranker import RerankerClient
from serein.application import Services
from serein.core.store import Store
from serein.deployment import save_settings
from serein.recall.index import build_index
from tests_aml.test_narrative_read_guards import published_volume


@pytest.fixture
def planned(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "_DATA_ROOT", tmp_path)
    monkeypatch.setattr(engine, "_EXPAND_ARCS", True)
    monkeypatch.setattr(engine, "_PLAN_ARCS", True)
    monkeypatch.setattr(engine, "_EXPAND_ENTITIES", False)
    monkeypatch.setattr(engine, "_rewrite_query", lambda *_: {"queries": ["Lin Vale"], "entities": []})
    monkeypatch.setattr(originals, "hits", lambda *_, **kwargs: [])
    paths = engine._paths("synthetic-user")
    with Store(paths.database) as store:
        store.create("anchor", "event", "Lin Vale employer", "Lin Vale joined Oriole Atelier.", manual_surface=True)
        store.create("bridge", "event", "Oriole current operating site", "Oriole Atelier moved to Valencia on May 2.", manual_surface=True)
        store.create("noise", "event", "Oriole storage colors", "Oriole Atelier storage shelves are orange.", manual_surface=True)
        store.create("excluded", "event", "Oriole private record", "EXCLUDED_BODY_ONLY", manual_surface=True,
                     metadata={"domain": "work"})
        store.create("volume", "narrative", "Oriole studio history", "UNSELECTED_VOLUME_BODY_ONLY",
                     metadata={"arc_key": "studio", "publication_status": "reviewed"})
        for key in ("anchor", "bridge", "noise", "excluded"):
            store.conn.execute("INSERT INTO narrative_materials VALUES ('volume',1,'test','event',?,'linked','{}')", (key,))
    build_index(paths.database, paths.index)
    save_settings(paths.database, {"models": [{"id": "synthetic-rerank", "model": "synthetic-rerank",
        "base_url": "https://mock.invalid/v1", "protocol": "openai", "api_key": "synthetic"}],
        "assignments": {"reranker": "synthetic-rerank"}, "recall": {"domains": {"work": "excluded"}}})
    observed = {"prompts": [], "reranks": []}

    def rank(self, query, docs):
        observed["reranks"].append([doc["ref"] for doc in docs])
        return {doc["ref"]: .99 if doc["ref"] == "anchor" else .01 for doc in docs}

    monkeypatch.setattr(RerankerClient, "__call__", rank)
    return paths, observed


def search(**kwargs):
    return engine.search_memory(query=kwargs.pop("query", "Where does Lin Vale currently work?"), options=None,
                                user_id=kwargs.pop("user_id", "synthetic-user"), top_k=kwargs.pop("top_k", 2), **kwargs)


def chooser(prompt, target="bridge", **changes):
    menus = json.loads(prompt.split("MENUS:\n", 1)[1])
    key = next(menu for menu in menus if any(item["id"] == target for item in menu["materials"]))
    pick = next(item["index"] for item in key["materials"] if item["id"] == target)
    return {"sufficient": False, "supports": [{"ref": "anchor", "quote": "Lin Vale joined Oriole Atelier."}],
            "missing": ["The current operating location of Lin Vale's employer"],
            "selections": [{"arc_key": key["arc_key"], "picks": [pick], "gap": 0}], **changes}


def reviewer(prompt, **changes):
    reads = json.loads(prompt.split("READS:\n", 1)[1])
    return {"accepted": [{"selection": item["selection"], "anchor_quote": item["anchor"]["text"],
                           "quote": item["material"]["text"]} for item in reads], **changes}


def test_enough_evidence_stops_before_read_and_reuses_ranking(planned, monkeypatch):
    _, observed = planned
    def enough(prompt):
        observed["prompts"].append(prompt)
        assert "Lin Vale joined Oriole Atelier." in prompt
        assert "UNSELECTED_VOLUME_BODY_ONLY" not in prompt
        assert "EXCLUDED_BODY_ONLY" not in prompt
        return {"sufficient": True, "supports": [{"ref": "anchor", "quote": "Lin Vale joined Oriole Atelier."}],
                "missing": [], "selections": []}
    monkeypatch.setattr(engine, "_model_json", enough)
    monkeypatch.setattr(Services, "arc_picks", lambda *_a, **_k: pytest.fail("Enough evidence must not read an Arc"))
    assert [row["id"] for row in search(query="Which employer did Lin Vale join?")] == ["anchor"]
    assert len(observed["prompts"]) == 1
    assert observed["reranks"] == [["anchor"]]
    assert search(user_id="other-user") == []


def test_missing_relation_reads_once_then_retains_low_similarity_joint_evidence(planned, monkeypatch):
    _, observed = planned
    def decide(prompt):
        observed["prompts"].append(prompt)
        if "READS:\n" in prompt:
            assert "Oriole Atelier moved to Valencia on May 2." in prompt
            return reviewer(prompt)
        return chooser(prompt)
    monkeypatch.setattr(engine, "_model_json", decide)
    rows = search()
    assert {row["id"] for row in rows} == {"anchor", "bridge"}
    assert all(set(row) == {"id", "content", "score", "created_at"} for row in rows)
    assert len(observed["prompts"]) == 2
    assert "UNSELECTED_VOLUME_BODY_ONLY" not in "\n".join(observed["prompts"])
    assert len(observed["reranks"]) == 2


def test_source_selection_keeps_bridge_without_repeating_full_review_quote(planned, monkeypatch):
    paths,_=planned
    with Store(paths.database) as store:
        doc=store.read('bridge')
        store.revise('bridge',expected_revision=doc['revision'],title=doc['title'],
                     body_md='Oriole Atelier moved to Valencia on May 2. Its shelves are orange.',metadata=doc['metadata'])
    from serein.recall.index import refresh_index
    refresh_index(paths.database,paths.index,['bridge'])
    monkeypatch.setenv('SEREIN_AML_SOURCE_EVIDENCE','1')
    def model(prompt):
        if '\nEVIDENCE: ' in prompt:
            packet=json.loads(prompt.split('\nEVIDENCE: ',1)[1])
            assert all(row['linked_refs'] for row in packet)
            return {'selections':[{'ref':row['ref'],'units':[row['units'][0]['id']]} for row in packet]}
        if 'READS:\n' in prompt:
            reads=json.loads(prompt.split('READS:\n',1)[1])
            return {'accepted':[{'selection':item['selection'],
                'anchor_quote':' '.join(unit['text'] for unit in item['anchor']['units']),
                'quote':' '.join(unit['text'] for unit in item['material']['units'])} for item in reads]}
        return reviewer(prompt) if 'READS:\n' in prompt else chooser(prompt)
    monkeypatch.setattr(engine,'_model_json',model)
    found=search()
    assert {r['id'] for r in found}=={'anchor','bridge'}
    assert 'Valencia' in str(found) and 'shelves' not in str(found)


def test_read_review_rejects_shared_topic_without_reserved_slots(planned, monkeypatch):
    _, observed = planned
    def decide(prompt):
        observed["prompts"].append(prompt)
        return {"accepted": []} if "READS:\n" in prompt else chooser(prompt, "noise")
    monkeypatch.setattr(engine, "_model_json", decide)
    assert [row["id"] for row in search()] == ["anchor"]
    assert len(observed["prompts"]) == 2
    assert observed["reranks"] == [["anchor"]]


@pytest.mark.parametrize("changes", [
    {"sufficient": "false"}, {"supports": []}, {"supports": [{"ref": "other-user", "quote": "invented"}]},
    {"supports": [{"ref": [], "quote": "invented"}]}, {"missing": []},
    {"selections": [{"arc_key": "studio", "picks": [2], "gap": True}]},
    {"supports": [{"ref": "anchor", "quote": "Lin Vale joined a different company."}]},
])
def test_invalid_assessment_does_not_read_material(planned, monkeypatch, changes):
    monkeypatch.setattr(engine, "_model_json", lambda prompt: chooser(prompt, **changes))
    monkeypatch.setattr(Services, "arc_picks", lambda *_a, **_k: pytest.fail("Invalid assessment cannot authorize reads"))
    assert [row["id"] for row in search()] == ["anchor"]


@pytest.mark.parametrize("accepted", [
    [{"selection": "invented", "anchor_quote": "Lin Vale joined Oriole Atelier.", "quote": "Oriole Atelier moved to Valencia on May 2."}],
    [{"selection": "s0", "anchor_quote": "invented anchor", "quote": "Oriole Atelier moved to Valencia on May 2."}],
    [{"selection": "s0", "anchor_quote": "Lin Vale joined Oriole Atelier.", "quote": "Oriole Atelier moved to Boston."}],
])
def test_review_requires_exact_visible_quotes_and_selection_ids(planned, monkeypatch, accepted):
    monkeypatch.setattr(engine, "_model_json", lambda prompt: {"accepted": accepted} if "READS:\n" in prompt else chooser(prompt))
    assert [row["id"] for row in search()] == ["anchor"]


def test_excluded_material_body_never_reaches_review_model(planned, monkeypatch):
    _, observed = planned
    def decide(prompt):
        observed["prompts"].append(prompt)
        assert "EXCLUDED_BODY_ONLY" not in prompt
        assert "READS:\n" not in prompt
        return chooser(prompt, "excluded")
    monkeypatch.setattr(engine, "_model_json", decide)
    assert [row["id"] for row in search()] == ["anchor"]
    assert len(observed["prompts"]) == 1


def test_post_review_source_change_still_suppresses_expansion(planned, monkeypatch):
    paths, _ = planned
    def decide(prompt):
        if "READS:\n" not in prompt:
            return chooser(prompt)
        result = reviewer(prompt)
        with Store(paths.database) as store:
            doc = store.read("bridge")
            store.revise("bridge", expected_revision=doc["revision"], title=doc["title"], body_md="Updated after review.")
        return result
    monkeypatch.setattr(engine, "_model_json", decide)
    assert [row["id"] for row in search()] == ["anchor"]


def test_planning_is_opt_in_and_old_menu_route_remains_available(planned, monkeypatch):
    monkeypatch.setattr(engine, "_PLAN_ARCS", False)
    def legacy(prompt):
        assert "CURRENT_EVIDENCE:" not in prompt and "READS:" not in prompt
        return {"selections": [{"arc_key": "studio", "picks": [2]}]}
    monkeypatch.setattr(engine, "_model_json", legacy)
    assert {row["id"] for row in search()} == {"anchor", "bridge"}


def test_long_current_evidence_is_bounded_and_marked(planned):
    paths, _ = planned
    with Store(paths.database) as store:
        doc = store.read("anchor")
        store.revise("anchor", expected_revision=doc["revision"], title=doc["title"], body_md="Visible text."*1000)
        doc = store.read("anchor")
    from serein.configured_models import effective_settings
    from serein.config import Settings
    rows = engine._planning_evidence(effective_settings(Settings(paths.database, paths.index)), "Lin Vale", {
        "anchor": {"id": "anchor", "kind": "event", "document": doc}}, {"anchor": 1}, 4, [])
    assert len(rows[0]["text"]) <= 8000 and rows[0]["truncated"]
    stale = {**doc, "revision": doc["revision"]-1}
    assert engine._planning_evidence(effective_settings(Settings(paths.database, paths.index)), "Lin Vale", {
        "anchor": {"id": "anchor", "kind": "event", "document": stale}}, {"anchor": 1}, 4, []) == []


@pytest.mark.parametrize("change", ["fresh", "domain", "source"])
def test_planned_volume_obeys_public_source_snapshot_before_review(published_volume, monkeypatch, change):
    paths, _ = published_volume
    monkeypatch.setattr(engine, "_PLAN_ARCS", True)
    prompts = []
    if change == "domain":
        save_settings(paths.database, {"recall": {"domains": {"work": "excluded"}}})
    elif change == "source":
        with Store(paths.database) as store:
            doc = store.read("scene_factory")
            store.revise(doc["id"], expected_revision=doc["revision"], title=doc["title"],
                         body_md="The meter was manufactured in CedarVale.", metadata=doc["metadata"])

    def decide(prompt):
        prompts.append(prompt)
        if "READS:\n" in prompt:
            assert change == "fresh"
            reads = json.loads(prompt.split("READS:\n", 1)[1])
            return {"accepted": [{"selection": item["selection"],
                "anchor_quote": "Dana installed the AsterBridge meter.",
                "quote": "The meter was manufactured in CopperBay."} for item in reads]}
        return {"sufficient": False, "supports": [{"ref": "scene_anchor", "quote": "Dana installed the AsterBridge meter."}],
                "missing": ["The factory of the installed meter"],
                "selections": [{"arc_key": "arc:device", "picks": [0], "gap": 0}]}
    monkeypatch.setattr(engine, "_model_json", decide)
    rows = engine.search_memory(query="Where was the AsterBridge meter manufactured?", options=None,
                                user_id="synthetic-user", top_k=4)
    assert ("narrative_device" in {row["id"] for row in rows}) is (change == "fresh")
    assert len(prompts) == (2 if change == "fresh" else 1)
    if change != "fresh":
        assert "CopperBay" not in "\n".join(prompts)
