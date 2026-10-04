"""One evidence-grounded search round, including when no Arc menu exists."""
import json

import pytest

from aml import arc_planning, engine, originals
from serein.adapters.reranker import RerankerClient
from serein.core.store import Store
from tests_aml.test_arc_planning import planned, reviewer, search

native_recall = engine._recall_hits

def proposal(**changes):
    return {"anchor": "anchor", "anchor_quote": "Lin Vale joined Oriole Atelier.",
            "entity": "Oriole Atelier", "query": "Oriole Atelier current operating location May 2",
            "gap": 0, **changes}


def decision(**changes):
    return {"sufficient": False, "supports": [{"ref": "anchor", "quote": "Lin Vale joined Oriole Atelier."}],
            "missing": ["The current operating location of Lin Vale's employer"], "selections": [],
            "searches": [proposal()], **changes}


@pytest.fixture
def gap_memory(planned, monkeypatch):
    paths, observed = planned
    monkeypatch.setattr(engine, "_EXPAND_GAPS", True)
    monkeypatch.setattr(engine, "_EXPAND_ARCS", False)
    monkeypatch.setattr(engine, "_PLAN_ARCS", False)
    monkeypatch.setattr(engine, "_EXPAND_ENTITIES", True)
    monkeypatch.setattr(engine, "_bridge_candidate", lambda *_: pytest.fail("No pre-assessment rule expansion"))
    observed["queries"] = []

    def recall(_services, text, **kwargs):
        observed["queries"].append(text)
        keys = ["anchor"] if text == "Lin Vale" else ["bridge", "noise", "excluded"]
        with Store(paths.database, read_only=True) as store:
            return [{"id": key, "kind": "event", "document": store.read(key)} for key in keys]

    def rank(_self, query, documents):
        assert "excluded" not in {item["ref"] for item in documents}
        observed["reranks"].append(query)
        return {item["ref"]: (.9 if item["ref"] == "anchor" or query.startswith("Oriole") and item["ref"] == "bridge" else .001)
                for item in documents}

    monkeypatch.setattr(engine, "_recall_hits", recall)
    monkeypatch.setattr(RerankerClient, "__call__", rank)
    return paths, observed


def test_no_menu_gap_search_keeps_joint_evidence_and_stops_after_one_round(gap_memory, monkeypatch):
    _, observed = gap_memory
    def model(prompt):
        observed["prompts"].append(prompt)
        assert "EXCLUDED_BODY_ONLY" not in prompt
        if "READS:\n" in prompt:
            reads = json.loads(prompt.split("READS:\n", 1)[1])
            assert len(reads) == 1 and reads[0]["bridge_entity"] == "Oriole Atelier"
            return reviewer(prompt)
        assert json.loads(prompt.split("MENUS:\n", 1)[1]) == []
        return decision()
    monkeypatch.setattr(engine, "_model_json", model)
    rows = search()
    assert {row["id"] for row in rows} == {"anchor", "bridge"}
    assert len(observed["prompts"]) == 2
    assert observed["queries"] == ["Lin Vale", proposal()["query"]]
    assert proposal()["query"] in observed["reranks"]
    assert search(user_id="other-user") == []


def test_sufficient_evidence_and_invalid_plan_do_not_search(gap_memory, monkeypatch):
    _, observed = gap_memory
    for answer in (decision(sufficient=True), decision(supports=[]), decision(searches=[proposal(entity="invented")])):
        monkeypatch.setattr(engine, "_model_json", lambda _prompt, answer=answer: answer)
        assert [row["id"] for row in search()] == ["anchor"]
    assert observed["queries"] == ["Lin Vale"] * 3


def test_review_rejection_does_not_admit_gap_candidate(gap_memory, monkeypatch):
    monkeypatch.setattr(engine, "_model_json", lambda prompt: {"accepted": []} if "READS:\n" in prompt else decision())
    assert [row["id"] for row in search()] == ["anchor"]


def test_gap_queries_run_through_native_public_lexical_recall(gap_memory, monkeypatch):
    monkeypatch.setattr(engine, "_recall_hits", native_recall)
    monkeypatch.setattr(engine, "_model_json", lambda prompt: reviewer(prompt) if "READS:\n" in prompt else decision())
    assert {row["id"] for row in search()} == {"anchor", "bridge"}


@pytest.mark.parametrize("invalidate", [False, True])
def test_pending_original_bridge_is_read_and_revalidated(gap_memory, monkeypatch, invalidate):
    paths, _ = gap_memory
    pending = [{"id": 99, "text": "Oriole Atelier moved to Valencia on May 2."}]
    monkeypatch.setattr(originals, "pending", lambda *_: pending)
    def recall(_services, text, **kwargs):
        if text == "Lin Vale":
            with Store(paths.database, read_only=True) as store:
                return [{"id": "anchor", "kind": "event", "document": store.read("anchor")}]
        return [{"id": "raw:99", "kind": "original", "content": pending[0]["text"], "created_at": None}]
    monkeypatch.setattr(engine, "_recall_hits", recall)
    def model(prompt):
        if "READS:\n" not in prompt:
            return decision()
        result = reviewer(prompt)
        if invalidate:
            pending.clear()
        return result
    monkeypatch.setattr(engine, "_model_json", model)
    rows = search()
    assert ("raw:99" in {row["id"] for row in rows}) is not invalidate


@pytest.mark.parametrize("change", ["target", "anchor"])
def test_post_review_change_invalidates_joint_path(gap_memory, monkeypatch, change):
    paths, _ = gap_memory
    def model(prompt):
        if "READS:\n" not in prompt:
            return decision()
        result = reviewer(prompt)
        key = "bridge" if change == "target" else "anchor"
        with Store(paths.database) as store:
            doc = store.read(key)
            store.revise(key, expected_revision=doc["revision"], title=doc["title"], body_md="Changed after review.")
        return result
    monkeypatch.setattr(engine, "_model_json", model)
    assert "bridge" not in {row["id"] for row in search()}


def test_review_quote_must_retain_bridge_identity(gap_memory, monkeypatch):
    monkeypatch.setattr(engine, "_model_json", lambda prompt: {"accepted": [{"selection": "s0",
        "anchor_quote": "Lin Vale joined Oriole Atelier.", "quote": "Valencia on May 2."}]}
        if "READS:\n" in prompt else decision())
    assert [row["id"] for row in search()] == ["anchor"]


def test_gap_search_is_opt_in(planned, monkeypatch):
    monkeypatch.setattr(engine, "_PLAN_ARCS", False)
    monkeypatch.setattr(engine, "_EXPAND_ARCS", False)
    monkeypatch.setattr(engine, "_model_json", lambda *_: pytest.fail("Optional route disabled"))
    assert [row["id"] for row in search()] == ["anchor"]


@pytest.mark.parametrize("changes", [
    {"anchor": "unknown"}, {"anchor_quote": "invented"}, {"entity": "invented"},
    {"entity": "O"}, {"query": "a different company location"}, {"query": "x" * 301},
    {"gap": True}, {"gap": 4}, {"anchor": []},
])
def test_ungrounded_queries_never_authorize_search(changes):
    evidence = [{"ref": "anchor", "text": "Lin Vale joined Oriole Atelier."}]
    result = arc_planning.assess(lambda _: decision(searches=[proposal(**changes)]), "Where?", None,
                                 evidence, {}, allow_search=True)
    assert result["searches"] == []


def test_plan_bounds_deduplicates_and_prefers_menu_for_same_gap():
    evidence = [{"ref": "anchor", "text": "Lin Vale joined Oriole Atelier."}]
    searches = [proposal(), proposal(), proposal(query="Oriole Atelier address May 2"),
                proposal(query="Oriole Atelier relocation history")]
    result = arc_planning.assess(lambda _: decision(searches=searches), "Where?", None, evidence, {}, allow_search=True)
    assert len(result["searches"]) == 2
    menus = {"studio": {"title": "Studio", "materials": [{"index": 2, "id": "bridge"}]}}
    result = arc_planning.assess(lambda _: decision(searches=searches,
        selections=[{"arc_key": "studio", "picks": [2], "gap": 0}]), "Where?", None, evidence, menus, allow_search=True)
    assert result["choices"] and result["searches"] == []
