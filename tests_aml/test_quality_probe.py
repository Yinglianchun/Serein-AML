"""Evidence diagnostics must not turn unrelated matches into a passing chain."""
import pytest

from evals.quality import measure, phrase_present, retrieval_mode
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

    engine = SimpleNamespace(_EXPAND_ARCS=False, _EXPAND_ENTITIES=True, _arc_selections=selector)
    menu = {"work": {"title": "Work", "materials": [{"index": 0, "id": "roll"},
                                                      {"index": 2, "id": "event"}]}}
    trace = []
    with retrieval_mode(engine, "materials", trace):
        assert engine._EXPAND_ARCS and not engine._EXPAND_ENTITIES
        assert engine._arc_selections("question", None, menu) == [("work", [2])]
        assert seen[0]["work"]["materials"] == [{"index": 2, "id": "event"}]
    assert menu["work"]["materials"][0]["index"] == 0
    assert trace == [{"arc_key": "work", "picks": [2], "selected_ids": ["event"]}]
    assert engine._arc_selections is selector
    assert not engine._EXPAND_ARCS and engine._EXPAND_ENTITIES


def test_ablation_restores_engine_after_failure():
    from types import SimpleNamespace
    selector = lambda *_: []
    engine = SimpleNamespace(_EXPAND_ARCS=True, _EXPAND_ENTITIES=True, _arc_selections=selector)
    with pytest.raises(RuntimeError):
        with retrieval_mode(engine, "base", []):
            assert not engine._EXPAND_ARCS
            raise RuntimeError("failed search")
    assert engine._EXPAND_ARCS and engine._EXPAND_ENTITIES
    assert engine._arc_selections is selector
