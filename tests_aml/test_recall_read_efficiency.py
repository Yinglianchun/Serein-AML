"""Synthetic checks for the two narrowly ported public read optimizations."""

import pytest

from serein.core.store import Store
from serein.core.reader import Reader
from serein.recall.rendering import _arcs, read_arc_picks


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "reads.db"
    with Store(path) as store:
        store.create("event_a", "event", "A", "current", revision=2, manual_surface=True)
        store._add_revision("event_a", 1, "Old", "old", {}, "fixture")
        store.bind("event_a", store.add_source("fixture", "source"))
        store.create("event_b", "event", "B", "unrelated", manual_surface=True)
        for name, event in [("a", "event_a"), ("b", "event_b")]:
            store.create("narrative_" + name, "narrative", name, "volume " + name,
                         revision=2, metadata={"arc_key": name})
            store._add_revision("narrative_" + name, 1, name, "old volume", {"arc_key": name}, "fixture")
            store.conn.execute("INSERT INTO narrative_materials VALUES (?,2,'fixture','event',?,'linked','{}')",
                               ("narrative_" + name, event))
    return path


def test_current_read_once_history_and_lifecycle_stay_fresh(database, monkeypatch):
    with Reader(database) as reader:
        original = reader.store.read
        original_surface = reader.store.surface_state
        checking_surface = False
        calls = []
        def surface_state(*args, **kwargs):
            nonlocal checking_surface
            checking_surface = True
            try:
                return original_surface(*args, **kwargs)
            finally:
                checking_surface = False
        def read(*args, **kwargs):
            # Surface eligibility has its own reads; preserve that real check.
            if not checking_surface:
                calls.append((args, kwargs))
            return original(*args, **kwargs)
        monkeypatch.setattr(reader.store, "read", read)
        monkeypatch.setattr(reader.store, "surface_state", surface_state)
        current = reader.read("event_a")
        assert len(calls) == 1
        assert current["document"]["body_md"] == "current"
        assert current["evidence"][0]["content"] == "source"
        calls.clear()
        old = reader.read("event_a", revision=1)
        assert len(calls) == 2
        assert old["document"]["body_md"] == "old" and old["evidence"] == []
        with Store(database) as store:
            store.set_lifecycle("event_a", "deleted")
        assert reader.read("event_a")["document"] is None


def test_menu_output_equal_but_unrelated_body_not_read(database, monkeypatch):
    with Reader(database) as reader:
        expected = _arcs(reader, [{"id": "event_a"}])
        original = reader.read
        calls = []
        def read(identifier, **kwargs):
            calls.append(identifier)
            return original(identifier, **kwargs)
        monkeypatch.setattr(reader, "read", read)
        assert _arcs(reader, [{"id": "event_a", "kind": "event"}]) == expected
        assert "event_b" not in calls
        explicit = read_arc_picks(reader, "b", [0])
        assert explicit["items"][0]["object"]["document"]["body_md"] == "volume b"


def test_append_exclusion_history_and_pagination_preserved(database):
    with Store(database) as store:
        store.conn.execute("INSERT INTO event_arc_links VALUES ('b','event_a','fixture','{}')")
    with Reader(database) as reader:
        owners, _ = _arcs(reader, [{"id": "event_a", "kind": "event"}])
        assert {item["arc_key"] for item in owners["event_a"]} == {"a", "b"}
        assert reader.materials("narrative_b", revision=1)["items"] == []
        first = reader.materials("narrative_b", limit=1)
        second = reader.materials("narrative_b", offset=first["next_offset"])
        assert first["items"] + second["items"] == reader.materials("narrative_b")["items"]
    with Store(database) as store:
        store.conn.execute("INSERT INTO narrative_materials VALUES ('narrative_b',2,'excluded','event','event_a','excluded','{}')")
    with Reader(database) as reader:
        assert _arcs(reader, [{"id": "event_a", "kind": "event"}]) == _arcs(reader, [{"id": "event_a"}])
        owners, _ = _arcs(reader, [{"id": "event_a", "kind": "event"}])
        assert [item["arc_key"] for item in owners["event_a"]] == ["a"]
