import sqlite3

import pytest

from serein.core import Conflict, Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "serein.db") as value:
        yield value


def setup_evidence(store):
    store.create("event_1", "event", "Event", "Event body")
    store.create("scene_1", "scene", "Scene", "Scene body")
    a = store.add_source("chat/session/message1", "first message")
    b = store.add_source("chat/session/message2", "second message")
    store.bind("event_1", a)
    store.bind("event_1", b)
    return a, b


def test_full_coverage_hides_event_without_archiving_or_revising(store):
    a, b = setup_evidence(store)
    store.bind("scene_1", a)
    assert store.surface_state("event_1")["can_surface"]
    original = store.read("event_1")
    store.bind("scene_1", b)
    assert store.surface_state("event_1") == {
        "document_id": "event_1", "can_surface": False,
        "reasons": ["covered_by_scene"], "covering_scene_ids": ["scene_1"],
    }
    assert store.read("event_1") == original


def test_unbinding_does_not_clear_manual_exclusion(store):
    a, b = setup_evidence(store)
    store.bind("scene_1", a)
    binding = store.bind("scene_1", b)
    store.set_manual_surface("event_1", False)
    store.unbind(binding)
    assert store.surface_state("event_1")["reasons"] == ["manual_disabled"]
    store.set_manual_surface("event_1", True)
    assert store.surface_state("event_1")["can_surface"]


def test_partial_scenes_cannot_be_combined_or_stale_snapshots_substituted(store):
    a, b = setup_evidence(store)
    store.create("scene_2", "scene", "Second", "body")
    store.bind("scene_1", a)
    store.bind("scene_2", b)
    edited_b = store.add_source("chat/session/message2", "edited message")
    store.bind("scene_1", edited_b)
    assert store.surface_state("event_1")["can_surface"]


def test_archiving_covering_scene_restores_only_eligible_events(store):
    a, b = setup_evidence(store)
    store.bind("scene_1", a)
    store.bind("scene_1", b)
    store.set_lifecycle("scene_1", "archived")
    assert store.surface_state("event_1")["can_surface"]
    store.set_lifecycle("event_1", "archived")
    store.set_lifecycle("scene_1", "active")
    store.set_lifecycle("scene_1", "deleted")
    assert store.surface_state("event_1")["reasons"] == ["archived"]
    with pytest.raises(Conflict):
        store.set_lifecycle("scene_1", "active")


def test_empty_evidence_does_not_count_as_coverage_and_unknown_is_not_enabled(store):
    store.create("event", "event", "E", "body", manual_surface=None)
    store.create("scene", "scene", "S", "body")
    assert store.surface_state("event")["reasons"] == ["manual_unreviewed"]
    store.set_manual_surface("event", True)
    assert store.surface_state("event")["can_surface"]


def test_reading_and_surface_checks_do_not_write(store):
    setup_evidence(store)
    before = store.conn.total_changes
    store.read("event_1")
    store.surface_state("event_1")
    assert store.conn.total_changes == before


def test_narrative_is_not_automatically_surfaced_as_a_scene(store):
    store.create("narrative", "narrative", "A roll", "body")
    assert store.read("narrative")["body_md"] == "body"
    assert store.surface_state("narrative")["reasons"] == ["narrative_requires_intent"]


def test_revision_roundtrip_and_stale_edit_rejected(store):
    store.create("scene", "scene", "title", "\n中文 **正文**\r\n")
    store.revise("scene", expected_revision=1, title="new", body_md="new body")
    with pytest.raises(Conflict):
        store.revise("scene", expected_revision=1, title="stale", body_md="bad")
    assert store.read("scene", revision=1)["body_md"] == "\n中文 **正文**\r\n"
    assert store.read("scene")["body_md"] == "new body"


def test_transaction_failure_rolls_back_documents_and_sources(store):
    with pytest.raises(sqlite3.IntegrityError), store.transaction():
        store.create("scene", "scene", "title", "body")
        store.bind("scene", "missing-source")
    assert store.read("scene") is None


def test_deletion_marker_blocks_recreation(store):
    store.record_deletion("old_scene", "2026-01-01", {"reason": "user deleted"})
    with pytest.raises(Conflict):
        store.create("old_scene", "scene", "title", "body")


def test_does_not_adopt_unrelated_database(tmp_path):
    path = tmp_path / "other.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE user_data (id INTEGER)")
    with pytest.raises(ValueError, match="non-Serein"):
        Store(path)
