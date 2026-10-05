"""Evidence diagnostics must not turn unrelated matches into a passing chain."""
import pytest

from evals.quality import measure, phrase_present, rescore, retrieval_mode, summarize
from evals.quality_fixture import fixture


def row(identifier, content):
    return {"id": identifier, "content": content}


def test_relationship_must_be_in_one_record_but_chain_can_cross_records():
    case = fixture()[1][0]
    separated = [row("person", "Lin Vale"), row("company", "Oriole Atelier"),
                 row("city", "Valencia May 2")]
    assert measure(case, separated)["coverage"] == 0
    connected = [row("person", "Lin Vale joined Oriole Atelier"),
                 row("city", "Oriole Atelier moved to Valencia on May 2")]
    result = measure(case, connected)
    assert result["complete_literal_coverage"]
    assert result["evidence"] == {"affiliation": ["person"], "latest-operating-site": ["city"]}


def test_previous_site_and_same_name_cousin_do_not_fill_latest_site_gap():
    case = fixture()[1][0]
    rows = [row("person", "Lin Vale joined Oriole Atelier"),
            row("old", "Oriole Atelier operated in Porto since March 11"),
            row("other", "Lin Moss joined Pineworks in Osaka")]
    result = measure(case, rows)
    assert result["missing_units"] == ["latest-operating-site"]
    assert result["distractor_units"] == {"cousin-employment": ["other"]}
    assert result["context_characters"] == sum(len(item["content"]) for item in rows)


@pytest.mark.parametrize("text,phrase,expected", [
    ("Valencia", "Valencia", True), ("valencia", "Valencia", True),
    ("NewValencia", "Valencia", False), ("ValenciaEast", "Valencia", False),
    ("May 20", "May 2", False), ("March\n22", "March 22", True),
    ("Lin Vale's", "Lin Vale", True),
    ("自5月2日起搬迁", "5月2日", True), ("自 5 月 2 日起搬迁", "5月2日", True),
    ("自5月20日起搬迁", "5月2日", False), ("自15月2日起搬迁", "5月2日", False),
])
def test_literal_phrase_boundaries(text, phrase, expected):
    assert phrase_present(text, phrase) is expected


def test_all_probe_evidence_exists_in_synthetic_input():
    sessions, cases = fixture()
    originals = [row(str(index), message["content"]) for index, message in enumerate(
        message for _, _, messages in sessions for message in messages)]
    assert len(originals) == 31
    assert len(sessions) == 6
    assert all(measure(case, originals)["complete_literal_coverage"] for case in cases)


def test_material_ablation_hides_volume_without_forcing_model_picks():
    from types import SimpleNamespace
    seen = []

    def selector(query, options, menus):
        seen.append(menus)
        return [("work", [2])]

    engine = SimpleNamespace(_EXPAND_ARCS=False, _EXPAND_ENTITIES=True, _EXPAND_GAPS=True, _arc_selections=selector,
                             arc_planning=SimpleNamespace(choose=lambda *_: []))
    menu = {"work": {"title": "Work", "materials": [{"index": 0, "id": "roll"},
                                                      {"index": 2, "id": "event"}]}}
    trace = []
    with retrieval_mode(engine, "materials", trace):
        assert engine._EXPAND_ARCS and not engine._EXPAND_ENTITIES and not engine._EXPAND_GAPS
        assert engine._arc_selections("question", None, menu) == [("work", [2])]
        assert seen[0]["work"]["materials"] == [{"index": 2, "id": "event"}]
    assert menu["work"]["materials"][0]["index"] == 0
    assert trace == [{"arc_key": "work", "picks": [2], "selected_ids": ["event"]}]
    assert engine._arc_selections is selector
    assert not engine._EXPAND_ARCS and engine._EXPAND_ENTITIES and engine._EXPAND_GAPS


def test_ablation_restores_engine_after_failure():
    from types import SimpleNamespace
    selector = lambda *_: []
    engine = SimpleNamespace(_EXPAND_ARCS=True, _EXPAND_ENTITIES=True, _EXPAND_GAPS=True, _arc_selections=selector,
                             arc_planning=SimpleNamespace(choose=lambda *_: []))
    with pytest.raises(RuntimeError):
        with retrieval_mode(engine, "base", []):
            assert not engine._EXPAND_ARCS
            raise RuntimeError("failed search")
    assert engine._EXPAND_ARCS and engine._EXPAND_ENTITIES and engine._EXPAND_GAPS
    assert engine._arc_selections is selector


def test_planned_material_ablation_keeps_current_evidence_and_hides_volume():
    from types import SimpleNamespace
    seen = []
    def planner(model, query, options, evidence, menus):
        seen.append((evidence, menus))
        return [{"arc_key": "work", "picks": [2], "missing": "A work location"}]
    engine = SimpleNamespace(_EXPAND_ARCS=False, _EXPAND_ENTITIES=True, _EXPAND_GAPS=True, _arc_selections=lambda *_: [],
                             arc_planning=SimpleNamespace(choose=planner))
    evidence = [{"ref": "anchor", "text": "Current evidence"}]
    menus = {"work": {"title": "Work", "materials": [{"index": 0, "id": "roll"}, {"index": 2, "id": "event"}]}}
    trace = []
    with retrieval_mode(engine, "materials", trace):
        assert engine.arc_planning.choose(None, "question", None, evidence, menus)
    assert seen[0][0] is evidence
    assert seen[0][1]["work"]["materials"] == [{"index": 2, "id": "event"}]
    assert trace[0]["selected_ids"] == ["event"]
    assert engine.arc_planning.choose is planner


def test_chinese_translation_preserves_evidence_units():
    case = fixture()[1][0]
    result = measure(case, [row("person", "Lin Vale在Oriole Atelier任职。"),
                            row("site", "Oriole Atelier自 5 月 2 日起在瓦伦西亚运营。")])
    assert result["complete_literal_coverage"]


def test_rescore_keeps_returned_text_and_original_gaps():
    cases = fixture()[1]
    rows = [row("person", "Lin Vale在Oriole Atelier任职。"),
            row("site", "Oriole Atelier自 5 月 2 日起在瓦伦西亚运营。")]
    results = [{"case": case["id"], "mode": mode, "rows": rows, **measure(case, [])}
               for case in cases for mode in ("base", "materials", "volumes")]
    report = {"kind": "synthetic_literal_evidence_ablation", "results": results,
              "summary": summarize(results, len(cases)), "memory_inventory": [],
              "authored_inventory_coverage": {}}
    reviewed = rescore(report)
    first = reviewed["results"][0]
    assert first["complete_literal_coverage"]
    assert first["initial_literal_measure"]["coverage"] == 0
    assert first["rows"] is rows
    assert reviewed["initial_summary"]["base"]["fully_covered_cases"] == 0
    assert reviewed["summary"]["base"]["fully_covered_cases"] == 1
    with pytest.raises(ValueError, match="complete result"):
        rescore({**report, "results": results[:-1]})
